"""Container policy and lifecycle regressions without Docker or real accounts."""
import json
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbridge.host.layout import config_root, sensitive_root
from wxbridge.host.settings import bind_host
from wxbridge.host.supervisor import Supervisor
from wxbridge.storage.memoryfs import is_noswap_tmpfs
from wxbridge_cli.compose import initialize, execute


class ContainerTests(unittest.TestCase):
    def test_host_network_migrates_web_to_loopback_and_uses_account_port(self):
        from wxbridge.host.container import prepare_web
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-tmp') as temporary:
            web=Path(temporary)/'web.json'
            web.write_text('{"host":"0.0.0.0","port":8730}')
            web.chmod(0o600)
            # The container writes web.json as root; ownership policy itself is covered in test_host_management.
            original_stat=Path.stat
            def root_owned(current,*args,**kwargs):
                info=original_stat(current,*args,**kwargs)
                return SimpleNamespace(st_uid=0,st_mode=info.st_mode) if current==web else info
            with patch.dict(os.environ,{'WXBRIDGE_RUNTIME':'container','WXBRIDGE_NETWORK':'host','WXBRIDGE_WEB_PORT':'18730'}),patch('os.fchown',create=True), \
                    patch.object(Path,'stat',root_owned):
                prepare_web(web)
                self.assertEqual(json.loads(web.read_text()),{'host':'127.0.0.1','port':18730})
                with patch.dict(os.environ,{'WXBRIDGE_WEB_PORT':'70000'}):
                    with self.assertRaisesRegex(ValueError,'web_port_invalid'):prepare_web(web)
                self.assertEqual(json.loads(web.read_text())['port'],18730)

    def test_host_network_does_not_allow_shared_pid_or_ipc(self):
        from wxbridge_cli.compose import read_project
        service={'init':True,'network_mode':'host'}
        with patch('pathlib.Path.read_text',side_effect=lambda **kw:json.dumps({'services':{'account':service}})):
            read_project(Path('.'))
            for name,value in (('pid','host'),('ipc','host'),('ports',['8730:8730'])):
                service[name]=value
                with self.assertRaises(ValueError):read_project(Path('.'))
                del service[name]

    def test_bootstrap_copies_legacy_provider_without_overwriting_root_file(self):
        from wxbridge.host.bootstrap import initialize_data
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-tmp') as temporary:
            root=Path(temporary)
            legacy=root/'data/secrets'
            legacy.mkdir(parents=True)
            (legacy/'provider.env').write_text('DEEPSEEK_API_KEY=synthetic-old')
            with patch('os.chown',create=True),patch('os.fchown',create=True):
                initialize_data(root/'data',root)
                self.assertEqual((root/'provider.env').read_text(),'DEEPSEEK_API_KEY=synthetic-old')
                (legacy/'provider.env').write_text('DEEPSEEK_API_KEY=synthetic-changed')
                initialize_data(root/'data',root)
                self.assertEqual((root/'provider.env').read_text(),'DEEPSEEK_API_KEY=synthetic-old')

    def test_compose_bootstrap_preserves_user_files(self):
        from wxbridge_cli.compose import initialize_data
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-tmp') as temporary:
            root=Path(temporary)/'data'
            with patch('os.chown',create=True),patch('os.fchown',create=True):
                initialize_data(root)
                password=(root/'settings/web-password').read_bytes()
                (root/'provider.env').chmod(0o600)
                (root/'provider.env').write_text('DEEPSEEK_API_KEY=synthetic')
                initialize_data(root)
            self.assertEqual((root/'settings/web-password').read_bytes(),password)
            self.assertEqual((root/'provider.env').read_text(),'DEEPSEEK_API_KEY=synthetic')

    def test_onboarding_is_disabled_outside_container_or_without_control(self):
        from web.backend.services import ServiceManager
        from wxbridge.management.repository import ManagementError
        with patch.dict(os.environ,{'WXBRIDGE_RUNTIME':'systemd'}):
            with self.assertRaises(ManagementError):ServiceManager(allow_control=True).onboarding('login')
        with patch.dict(os.environ,{'WXBRIDGE_RUNTIME':'container'}):
            with self.assertRaises(ManagementError):ServiceManager(allow_control=False).onboarding('bind')

    def test_binding_rejects_active_bot_before_reading_account_data(self):
        from wxbridge.host.onboarding import bind_configured_account
        with self.assertRaisesRegex(ValueError,'stop_bot'):
            bind_configured_account(SimpleNamespace(enabled=True))

    def test_provider_source_cannot_escape_read_only_mount(self):
        from wxbridge.host.credentials import validate_provider
        with self.assertRaisesRegex(ValueError, 'unsafe_provider_secret_source'):
            validate_provider('/tmp/provider.env')

    def test_provider_is_read_directly_and_requires_service_read_access(self):
        from wxbridge.host.credentials import validate_provider
        source = Path('/run/secrets/wxbridge/provider.env')
        info = SimpleNamespace(st_mode=0o100440,st_uid=0,st_gid=10001,st_size=20)
        parent = SimpleNamespace(st_mode=0o40750,st_uid=0,st_gid=10001)
        with patch('pathlib.Path.is_symlink',return_value=False), \
             patch('pathlib.Path.stat',return_value=parent), \
             patch('os.O_NOFOLLOW',0x20000,create=True), \
             patch('os.O_NONBLOCK',0x800,create=True), \
             patch('os.open',return_value=123), \
             patch('os.fdopen') as opened, patch('os.fstat',return_value=info), \
             patch('wxbridge.host.credentials.service_account',return_value=SimpleNamespace(pw_uid=10001,pw_gid=10001)):
            self.assertEqual(validate_provider(source),source)
            opened.return_value.__enter__.return_value.read.assert_not_called()
            for mode, group in ((0o100600,0),(0o100444,10001)):
                info.st_mode,info.st_gid = mode,group
                with self.assertRaisesRegex(ValueError,'provider.env must be private'):
                    validate_provider(source)

    def test_only_nonsecret_connection_overrides_are_inherited(self):
        from wxbridge.host.layout import environment
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'container',
                                    'OPENAI_BASE_URL':'http://host.docker.internal:8317/v1',
                                    'OPENAI_API_KEY':'synthetic-private-value'}):
            self.assertIn('OPENAI_BASE_URL', environment())
            self.assertNotIn('OPENAI_API_KEY', environment())

    def test_sensitive_mount_is_stricter_than_runtime_and_nested_disk_is_rejected(self):
        mounts = '1 0 0:1 / /run rw - tmpfs tmpfs rw\n2 1 0:2 / /run/disk rw - ext4 /dev/x rw'
        self.assertFalse(is_noswap_tmpfs(Path('/run/keys'), mounts))
        self.assertTrue(is_noswap_tmpfs(Path('/run/queue'), mounts, require_noswap=False))
        self.assertFalse(is_noswap_tmpfs(Path('/run/disk/queue'), mounts, require_noswap=False))

    def test_container_layout_and_bind_policy_do_not_change_native_defaults(self):
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'container'}):
            self.assertEqual(config_root(),Path('/var/lib/wxbridge-config'))
            self.assertEqual(sensitive_root(),Path('/run/wxbridge-sensitive'))
            self.assertEqual(bind_host('0.0.0.0'),'0.0.0.0')
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'systemd'}):
            self.assertEqual(config_root(),Path('/opt/wxbridge'))
            with self.assertRaises(ValueError):bind_host('0.0.0.0')

    def test_project_never_overwrites_existing_data(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-tmp') as temporary:
            root=Path(temporary)/'account'
            with patch('wxbridge_cli.compose.os.chown',create=True), patch('wxbridge_cli.compose.os.geteuid',return_value=0,create=True):
                initialize(root,'wxbridge:test',18730)
            saved=(root/'settings/web-password').read_bytes()
            with patch('wxbridge_cli.compose.os.geteuid',return_value=0,create=True):
                initialize(root,'wxbridge:test',18730)
                with self.assertRaises(ValueError):initialize(root,'other:image',18731)
            self.assertEqual((root/'settings/web-password').read_bytes(),saved)
            config=json.loads((root/'compose.json').read_text())
            service=config['services']['account']
            self.assertNotIn('privileged',service)
            self.assertNotIn('pid',service)
            self.assertNotIn('ports',service)
            self.assertEqual(service['network_mode'],'host')
            self.assertEqual(service['environment'],{'WXBRIDGE_RUNTIME':'container','WXBRIDGE_NETWORK':'host','WXBRIDGE_WEB_PORT':'18730'})
            self.assertNotIn('extra_hosts',service)
            from provider.config import load_config
            providers = load_config(root/'provider.env',environ={})
            for connection in providers.connections.values():
                self.assertIsNone(connection.proxy_url)
                self.assertTrue(connection.base_url.startswith('https://'))
            self.assertIn('noswap',config['volumes']['sensitive']['driver_opts']['o'])

    def test_nested_compose_dispatch_is_rejected(self):
        args=SimpleNamespace(project_dir=Path('.'),command='start')
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'container'}), patch('subprocess.run') as run:
            self.assertEqual(execute(args,[]),1)
            run.assert_not_called()

    def test_dispatch_removes_host_backend_flags(self):
        args=SimpleNamespace(project_dir=Path('.'),command='bot')
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'systemd'}), patch('pathlib.Path.read_text', return_value='{"services":{"account":{"init":true}}}'), patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(execute(args,['--backend','compose','--project-dir','.','bot','apply']),0)
            self.assertEqual(run.call_args.args[0][-3:],['wxbridge','bot','apply'])

    def test_stopped_status_is_one_json_document(self):
        args=SimpleNamespace(project_dir=Path('.'),command='status',json=True)
        with patch.dict(os.environ, {'WXBRIDGE_RUNTIME':'systemd'}), \
             patch('pathlib.Path.read_text', return_value='{"services":{"account":{"init":true}}}'), \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='[{"State":"exited"}]')), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(execute(args,[]),0)
            self.assertIsNone(json.loads(output.getvalue())['application'])

    @patch('signal.SIGKILL', 9, create=True)
    def test_helper_is_not_killed_before_wechat_disappears(self):
        supervisor=Supervisor.__new__(Supervisor)
        child=Mock(process=object())
        supervisor.children={'sender':child}
        supervisor.account=SimpleNamespace(pw_uid=10001)
        supervisor.helper_stop_started=0
        supervisor.wechat_stop_started=None
        with patch('wxbridge.host.supervisor.time.monotonic',return_value=31), \
             patch('wxbridge.host.processes.wechat_processes',return_value=[123]), \
             patch('wxbridge.host.processes.signal_pid') as send:
            supervisor.drain_container_helper()
            send.assert_called_once_with(123,signal.SIGTERM)
            child._signal.assert_not_called()
            self.assertTrue(supervisor.forced_shutdown)
        with patch('wxbridge.host.supervisor.time.monotonic',return_value=47), \
             patch('wxbridge.host.processes.wechat_processes',return_value=[]):
            supervisor.drain_container_helper()
            child._signal.assert_called_once_with(signal.SIGKILL)

    def test_namespace_cleanup_rejects_host_init(self):
        from wxbridge.host.processes import validate_namespace
        with patch('wxbridge.host.processes.Path.resolve',return_value=Path('/sbin/systemd')):
            with self.assertRaises(ValueError):validate_namespace()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux pidfd shutdown ordering')
    def test_stuck_helper_survives_until_client_process_is_dead(self):
        from wxbridge.host.supervisor import Child
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        processes = []
        try:
            for _ in range(2):
                process = subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,
                                           text=True,start_new_session=True)
                processes.append(process)
                self.assertEqual(process.stdout.readline().strip(),'ready')
            client, helper = processes
            child = Child('sender',[],timeout=None)
            child.process = helper
            child.stop()
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.children = {'sender':child}
            supervisor.account = SimpleNamespace(pw_uid=os.geteuid())
            supervisor.helper_stop_started = 0
            supervisor.wechat_stop_started = None
            def clients(_uid):
                return [client.pid] if client.poll() is None else []
            with patch('wxbridge.host.processes.wechat_processes',side_effect=clients), \
                 patch('wxbridge.host.supervisor.time.monotonic',side_effect=[31,47,48]):
                supervisor.drain_container_helper()
                self.assertIsNone(helper.poll())
                supervisor.drain_container_helper()
                self.assertEqual(client.wait(timeout=5),-signal.SIGKILL)
                self.assertIsNone(helper.poll())
                supervisor.drain_container_helper()
                self.assertEqual(helper.wait(timeout=5),-signal.SIGKILL)
            self.assertTrue(supervisor.forced_shutdown)
        finally:
            for process in processes:
                if process.poll() is None:process.kill()
                process.wait(timeout=5)
                process.stdout.close()
