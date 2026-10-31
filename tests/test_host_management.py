"""Fresh-host plans and CLI lifecycle semantics; no apt, native sends or model calls."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge_service import cli, install, setup
from wxbridge_service.host import Paths, atomic_file, private_json

ROOT = Path(__file__).resolve().parents[1]


def status(*, enabled=True, pid=21, state='active'):
    return dict(bot_enabled=enabled, phase='listening' if enabled else 'stopped',
        components=dict(bot=dict(state=state,pid=pid), wechat=dict(state='active',pid=11),
                        sender=dict(state='active',pid=12), web=dict(state='active',pid=13)))


class HostCLITests(unittest.TestCase):
    def call(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch.object(cli,'validate_start'):
            return cli.main(args)

    def test_stop_uses_the_supervisor_and_waits_without_stopping_wechat(self):
        values = [status(), dict(applied=True,message='stopping'), status(enabled=False,pid=0,state='inactive')]
        with patch.object(cli,'control_request',side_effect=values) as request, patch.object(cli,'system_service') as system:
            self.assertEqual(self.call(['stop','--wait']),0)
        self.assertEqual([c.args[0] for c in request.call_args_list],['status','stop','status'])
        system.assert_not_called()

    def test_restart_keeps_a_paused_bot_paused(self):
        values = [status(enabled=False,pid=0,state='inactive'), dict(applied=False,message='paused')]
        with patch.object(cli,'control_request',side_effect=values) as request, patch.object(cli,'system_service') as system:
            self.assertEqual(self.call(['restart','--wait']),0)
        self.assertEqual([c.args[0] for c in request.call_args_list],['status','apply'])
        system.assert_not_called()

    def test_start_bootstraps_only_the_fixed_service_when_socket_is_absent(self):
        values = [FileNotFoundError(),status(enabled=False,pid=0,state='inactive'),dict(applied=True,message='starting')]
        with patch.object(cli,'control_request',side_effect=values), patch.object(cli,'system_service',return_value=SimpleNamespace(returncode=0)) as service:
            self.assertEqual(self.call(['start']),0)
        service.assert_called_once_with('start')

    def test_whole_service_operations_are_explicit_and_fixed(self):
        with patch.object(cli,'need_root') as root, patch.object(cli.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(cli.system_service('stop').returncode,0)
        root.assert_called_once()
        self.assertEqual(run.call_args.args[0],['systemctl','stop','wxbridge.service'])

    def test_instance_control_drops_to_the_service_account(self):
        args = cli.parser().parse_args(['instance','stop','alpha'])
        with patch.object(cli,'as_user',return_value=SimpleNamespace(returncode=0,stdout='{"enabled":false}')) as run, redirect_stdout(io.StringIO()):
            cli.instance_action(args,Paths())
        command = run.call_args.args[1]
        self.assertEqual(command[-2:],['stop','alpha'])
        self.assertNotIn('shell',run.call_args.kwargs)

    def test_status_rejects_stale_instance_snapshots(self):
        with patch.object(cli,'control_request',return_value=status(pid=21)), patch.object(cli,'private_json',return_value=dict(pid=99,updated_at=0,instances={'old':{'cursor':123}})):
            value = cli.current_status(Paths())
        self.assertFalse(value['snapshot_current'])
        self.assertEqual(value['instances'],{})

    def test_logs_accept_only_known_components(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(['logs','--component','../../provider/.env'])

    def test_status_does_not_reuse_a_different_wechat_process_login(self):
        with patch.object(Path,'read_text',return_value='123'), patch.object(cli,'private_json',return_value=dict(pid=99,phase='logged_in')):
            self.assertEqual(cli.login_snapshot(Paths())['phase'],'unknown')

    def test_current_login_state_has_priority_over_a_legacy_snapshot(self):
        with patch.object(Path,'read_text',return_value='123'), patch.object(cli,'private_json',return_value=dict(process={'pid':123},phase='qr_required')):
            self.assertEqual(cli.login_snapshot(Paths())['phase'],'qr_required')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_source_copy_excludes_credentials_runtime_and_dependency_trees(self):
        source = self.root/'source'
        source.mkdir()
        (source/'scripts').mkdir()
        (source/'scripts/check-repository.py').write_bytes((ROOT/'scripts/check-repository.py').read_bytes())
        names = ['config.json','src/provider/.env','instance/a/config.json','src/webui/node_modules/lib.js',
                 'src/provider/build/old.py','src/provider/__pycache__/old.pyc','src/provider/models.json',
                 'src/webui/dist/index.html','src/provider/client.py','.env.example']
        for name in names:
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('synthetic')
        values={p.as_posix() for p in install.public_files(source)}
        self.assertIn('src/webui/dist/index.html',values)
        self.assertIn('src/provider/client.py',values)
        self.assertIn('.env.example',values)
        self.assertEqual(values.intersection(names),{'src/webui/dist/index.html','src/provider/client.py','.env.example'})

    def test_template_never_restores_api_credentials(self):
        raw=b'GEMINI_API_KEY=PRIVATE\nGEMINI_BASE_URL=http://127.0.0.1:8317/v1\nOPENAI_API_KEY=PRIVATE\n'
        result=install.credential_free_template(raw)
        self.assertNotIn(b'PRIVATE',result)
        self.assertIn(b'GEMINI_BASE_URL=http://127.0.0.1:8317/v1',result)

    def test_unit_rendering_uses_new_machine_identity_and_specific_lan_address(self):
        account=SimpleNamespace(pw_uid=912,pw_gid=913)
        mount,service=install.render_units(ROOT,account,'192.168.8.20')
        self.assertIn('uid=912,gid=913,noswap',mount)
        self.assertNotIn('@UID@',mount)
        self.assertIn('-m wxbridge_service --root /opt/wxbridge --web-host 192.168.8.20',service)
        self.assertIn('TimeoutStopSec=infinity',service)
        self.assertIn('SendSIGKILL=no',service)

    def test_new_installation_is_paused_and_has_no_bound_database_or_models(self):
        cfg=install.initial_config(Paths())
        self.assertEqual(cfg['db_dir'],'')
        self.assertEqual(cfg['native_api']['allowed_chat_ids'],['filehelper'])
        self.assertNotIn('models',cfg)
        self.assertNotIn('instances',cfg)
        self.assertEqual(Path(cfg['memory']['directory']),Paths().home/'memory')

    def test_existing_installation_is_rejected_before_any_install_action(self):
        args=SimpleNamespace(web_host='127.0.0.1',dry_run=False)
        run=Mock()
        with patch.object(install,'validate_host'), patch.object(install,'existing_targets',return_value=[Path('/opt/wxbridge')]):
            with self.assertRaisesRegex(ValueError,'拒绝覆盖'):
                install.install(ROOT,args,run=run)
        run.assert_not_called()

    def test_dry_run_never_creates_users_mounts_or_prompts_for_password(self):
        args=SimpleNamespace(web_host='127.0.0.1',wechat_deb=None,frida_wheel=None,with_vnc=False,dry_run=True)
        run=Mock()
        with patch.object(install,'validate_host'), patch.object(install,'existing_targets',return_value=[]), \
             patch.object(install,'verify_media',return_value=({},'fake')), patch.object(install,'public_files',return_value=[]), \
             patch.object(install,'verify_source',return_value=False), patch.object(install,'password_input') as password, \
             patch.object(Path,'is_file',return_value=True), redirect_stdout(io.StringIO()):
            plan=install.install(ROOT,args,run=run)
        self.assertFalse(plan['initial_bot_enabled'])
        run.assert_not_called();password.assert_not_called()

    def test_release_manifest_tampering_is_rejected(self):
        file=self.root/'config.defaults.json';file.write_text('{}')
        entry={'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'mode':'100644'}
        (self.root/'release-manifest.json').write_text(json.dumps(dict(format=1,files={'config.defaults.json':entry})))
        files=[Path('config.defaults.json'),Path('release-manifest.json')]
        self.assertTrue(install.verify_source(self.root,files))
        file.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'校验失败'):
            install.verify_source(self.root,files)

    def test_missing_locked_requirements_fails_before_any_host_changes(self):
        frontend=self.root/'src/webui/dist/index.html'
        frontend.parent.mkdir(parents=True)
        frontend.write_text('synthetic frontend')
        args=SimpleNamespace(web_host='127.0.0.1',wechat_deb=None,frida_wheel=None,with_vnc=False,dry_run=False)
        run=Mock()
        with patch.object(install,'validate_host'), patch.object(install,'existing_targets',return_value=[]), \
             patch.object(install,'verify_media',return_value=({},'fake')), patch.object(install,'public_files',return_value=[]), \
             patch.object(install,'verify_source',return_value=False), patch.object(install,'password_input') as password:
            with self.assertRaisesRegex(ValueError,'requirements.txt'):
                install.install(self.root,args,run=run)
        run.assert_not_called()
        password.assert_not_called()

    def test_release_inventory_keeps_all_committed_metadata_for_future_upgrades(self):
        (self.root/'scripts').mkdir()
        (self.root/'scripts/check-repository.py').write_bytes((ROOT/'scripts/check-repository.py').read_bytes())
        (self.root/'Open-Project.cmd').write_text('synthetic')
        entries={p.relative_to(self.root).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':'100644'}
                 for p in self.root.rglob('*') if p.is_file()}
        (self.root/'release-manifest.json').write_text(json.dumps(dict(format=1,files=entries)))
        files=install.public_files(self.root)
        self.assertIn(Path('Open-Project.cmd'),files)
        self.assertTrue(install.verify_source(self.root,files))

    def test_password_is_not_an_argument_and_requires_a_private_input_file(self):
        with self.assertRaisesRegex(ValueError,'12'):
            install.validate_password('short')
        with patch.object(install.sys.stdin,'isatty',return_value=False):
            with self.assertRaisesRegex(ValueError,'web-password-file'):
                install.password_input()


class BindingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp')
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        self.paths=Paths(app=root/'app',runtime=root/'run',home=root/'home',helper=root/'helper',settings=root/'boot.json')
        for path in (self.paths.app,self.paths.home,self.paths.helper):path.mkdir()
        self.config={'db_dir':'old','native_api':{'allowed_chat_ids':['filehelper']}}
        self.helper={'uid':123,'allowed_chat_ids':['filehelper']}
        for path,value in ((self.paths.app/'config.json',self.config),(self.paths.helper/'helper-config.json',self.helper),(self.paths.settings,{'db_dir':'old'})):
            path.write_text(json.dumps(value))
        self.account=SimpleNamespace(pw_uid=123,pw_gid=124,pw_dir=str(self.paths.home))

    def test_binding_requires_a_paused_bot_before_reading_or_writing_config(self):
        with patch.object(setup,'need_root'), patch.object(setup,'control_request',return_value=status()), patch.object(setup,'service_account') as account:
            with self.assertRaisesRegex(ValueError,'stop --wait'):
                setup.bind_account(Paths())
        account.assert_not_called()

    def test_foreign_or_ambiguous_chat_identifiers_are_rejected(self):
        with patch.object(setup,'need_root'), patch.object(setup,'control_request') as control:
            with self.assertRaisesRegex(ValueError,'ID'):
                setup.bind_account(Paths(),chat='群聊昵称')
        control.assert_not_called()

    def binding_patches(self):
        def write(path,data,**kwargs):
            Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_bytes(data)
        def write_json(path,data,**kwargs):write(path,json.dumps(data).encode())
        from contextlib import ExitStack
        stack=ExitStack()
        stack.enter_context(patch.object(setup,'need_root'))
        stack.enter_context(patch.object(setup,'control_request',return_value=status(enabled=False,pid=0,state='inactive')))
        stack.enter_context(patch.object(setup,'service_account',return_value=self.account))
        stack.enter_context(patch.object(setup,'checked_database',return_value=self.paths.home/'account/db_storage'))
        stack.enter_context(patch.object(setup,'atomic_file',side_effect=write))
        stack.enter_context(patch.object(setup,'atomic_json',side_effect=write_json))
        return stack

    def test_binding_updates_account_paths_but_keeps_provider_credentials_and_pause(self):
        private=self.paths.app/'provider.env';private.write_text('SYNTHETIC_PRIVATE_VALUE')
        with self.binding_patches(), patch.object(setup,'as_user') as child:
            result=setup.bind_account(self.paths,'/synthetic/db_storage')
        self.assertTrue(result['bound']);self.assertFalse(result['bot_started'])
        self.assertEqual(json.loads((self.paths.app/'config.json').read_text())['db_dir'],str(self.paths.home/'account/db_storage'))
        self.assertEqual(private.read_text(),'SYNTHETIC_PRIVATE_VALUE')
        child.assert_not_called()

    def test_failed_instance_creation_restores_all_binding_and_allowlist_files(self):
        paths=[self.paths.app/'config.json',self.paths.helper/'helper-config.json',self.paths.settings]
        before={p:p.read_bytes() for p in paths}
        with self.binding_patches(), patch.object(setup,'as_user',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError,'回退'):
                setup.bind_account(self.paths,'/synthetic/db_storage',chat='12345@chatroom',identifier='alpha')
        self.assertEqual({p:p.read_bytes() for p in paths},before)

    def test_created_instance_is_disabled_but_ready_for_explicit_model_selection(self):
        def child(paths,args,**kwargs):
            directory=self.paths.app/'instance/alpha';directory.mkdir(parents=True)
            (directory/'config.json').write_text(args[-1])
            return SimpleNamespace(returncode=0)
        with self.binding_patches(), patch.object(setup,'as_user',side_effect=child), patch.object(setup,'restart_sender'):
            result=setup.bind_account(self.paths,'/synthetic/db_storage',chat='12345@chatroom',identifier='alpha')
        value=json.loads((self.paths.app/'instance/alpha/config.json').read_text())
        self.assertTrue(result['instance_created']);self.assertFalse(value['enabled'])
        self.assertTrue(value['participation']['enabled'])
        self.assertFalse(value['search']['enabled']);self.assertFalse(value['image_generation']['enabled'])


if __name__=='__main__':
    unittest.main()
