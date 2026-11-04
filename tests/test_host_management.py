"""Fresh-host plans and CLI lifecycle semantics; no apt, native sends or model calls."""
from wxbridge.host.settings import WebConfig
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

from wxbridge_cli import cli, install
from wxbridge.host import binding as setup
from wxbridge.host.system import Paths, atomic_file, private_json

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
            self.assertEqual(self.call(['bot','stop','--wait']),0)
        self.assertEqual([c.args[0] for c in request.call_args_list],['status','stop','status'])
        system.assert_not_called()

    def test_restart_keeps_a_paused_bot_paused(self):
        values = [status(enabled=False,pid=0,state='inactive'), dict(applied=False,message='paused')]
        with patch.object(cli,'control_request',side_effect=values) as request, patch.object(cli,'system_service') as system:
            self.assertEqual(self.call(['bot','restart','--wait']),0)
        self.assertEqual([c.args[0] for c in request.call_args_list],['status','apply'])
        system.assert_not_called()

    def test_start_bootstraps_only_the_fixed_service_when_socket_is_absent(self):
        values = [FileNotFoundError(),status(enabled=False,pid=0,state='inactive'),dict(applied=True,message='starting')]
        with patch.object(cli,'control_request',side_effect=values), patch.object(cli,'system_service',return_value=SimpleNamespace(returncode=0)) as service:
            self.assertEqual(self.call(['bot','start']),0)
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

    def test_top_level_commands_only_control_the_whole_service(self):
        for action in ('start','stop','restart','enable','disable'):
            with self.subTest(action=action), patch.object(cli,'system_service',return_value=SimpleNamespace(returncode=0)) as system, \
                    patch.object(cli,'control_request') as control:
                self.assertEqual(self.call([action]),0)
                system.assert_called_once_with(action);control.assert_not_called()

    def test_removed_commands_are_not_silent_aliases(self):
        for command in ('service','apply'):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):cli.parser().parse_args([command])

    def test_web_control_never_bootstraps_the_whole_service(self):
        with patch.object(cli,'need_root'),patch.object(cli,'control_request',side_effect=FileNotFoundError()), \
                patch.object(cli,'system_service') as system:
            self.assertEqual(self.call(['web','start']),1)
        system.assert_not_called()

    def test_web_restart_waits_for_new_ready_pid_and_generation(self):
        before=dict(enabled=True,pid=10,state='active')
        after=dict(enabled=True,pid=11,state='active',ready=True,pending=False,generation=2)
        values=[dict(web=before),dict(message='accepted',generation=2),dict(web=after)]
        with patch.object(cli,'need_root'),patch.object(cli,'control_request',side_effect=values) as control:
            self.assertEqual(self.call(['web','restart','--wait']),0)
        self.assertEqual([c.args[0] for c in control.call_args_list],['status','web_restart','status'])

    def test_web_wait_timeout_is_not_reported_as_success(self):
        value=dict(web=dict(enabled=True,pid=10,state='active',ready=False,pending=True,generation=0))
        with patch.object(cli,'control_request',return_value=value), \
                patch.object(cli.time,'monotonic',side_effect=[0,0,3]),patch.object(cli.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'超时'):cli.wait_web('web_restart',2,10)

    def test_config_reload_keeps_disabled_web_stopped(self):
        values=[dict(web=dict(enabled=False,pid=0)),dict(generation=2)]
        with patch.object(cli,'control_request',side_effect=values) as control,patch.object(cli,'wait_web') as wait,redirect_stdout(io.StringIO()):
            cli.reload_web(30)
        wait.assert_not_called()
        self.assertEqual([c.args[0] for c in control.call_args_list],['status','web_reload'])

    def test_failed_config_reload_reports_saved_but_not_applied(self):
        values=[dict(web=dict(enabled=True,pid=10)),dict(generation=2)]
        with patch.object(cli,'control_request',side_effect=values),patch.object(cli,'wait_web',side_effect=ValueError('timeout')):
            with self.assertRaisesRegex(ValueError,'已保存但未生效'):cli.reload_web(30)

    def test_failed_control_connection_does_not_claim_pending_settings_are_successful(self):
        for error in (TimeoutError(),PermissionError(),ValueError('invalid-response')):
            with self.subTest(error=type(error).__name__),patch.object(cli,'control_request',side_effect=error):
                with self.assertRaisesRegex(ValueError,'已保存但未生效'):cli.reload_web(30)

    def test_missing_supervisor_saves_settings_for_next_start(self):
        with patch.object(cli,'control_request',side_effect=FileNotFoundError()), \
                redirect_stdout(io.StringIO()) as output:
            cli.reload_web(30)
        self.assertIn('下次启动',output.getvalue())

    def test_web_status_remains_available_when_saved_settings_are_invalid(self):
        value=dict(enabled=True,state='active',pid=13,effective=dict(host='127.0.0.1',port=8730),
                   pending=True,error='web_settings_invalid')
        with patch.object(cli,'read_web',side_effect=ValueError('invalid_json')), \
                patch.object(cli,'control_request',return_value=dict(web=value)):
            result=cli.web_status()
        self.assertEqual(result['pid'],13)
        self.assertIsNone(result['saved'])
        self.assertEqual(result['error'],'web_settings_invalid')


class WebSettingsTests(unittest.TestCase):
    def test_settings_reject_public_wildcard_and_invalid_types(self):
        from wxbridge.host.settings import validate_web
        for host in ('0.0.0.0','::','8.8.8.8','example.com',None,True):
            with self.subTest(host=host),self.assertRaises(ValueError):validate_web(dict(host=host,port=8730))
        for port in (0,65536,True,'8730'):
            with self.subTest(port=port),self.assertRaises(ValueError):validate_web(dict(host='127.0.0.1',port=port))
        self.assertEqual(validate_web(dict(host='localhost',port=8730)),WebConfig())

    def test_state_upgrade_preserves_component_switches_and_rejects_unknown_fields(self):
        from wxbridge.host.settings import migrate_state
        self.assertEqual(migrate_state(dict(schema=1,bot_enabled=False)),dict(schema=2,bot_enabled=False,web_enabled=True))
        current=dict(schema=2,bot_enabled=True,web_enabled=False)
        self.assertEqual(migrate_state(current),current)
        for value in (dict(schema=True,bot_enabled=False),dict(schema=1,bot_enabled=0),dict(current,extra=True)):
            with self.assertRaises(ValueError):migrate_state(value)

    def test_cli_configuration_is_saved_without_starting_a_stopped_web(self):
        from wxbridge.host.settings import read_web
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp') as directory:
            path=Path(directory)/'web.json'
            path.write_text('{"host":"127.0.0.1","port":8730}');path.chmod(0o600)
            from wxbridge.host import settings
            def read(value):return settings.validate_web(json.loads(Path(value).read_text()))
            def write(value,data):Path(value).write_text(json.dumps(data))
            with patch.object(cli,'WEB_CONFIG',path),patch.object(cli,'need_root'), \
                    patch.object(cli,'read_web',side_effect=read),patch.object(settings,'read_web',side_effect=read), \
                    patch.object(settings,'atomic_json',side_effect=write), \
                    patch.object(cli,'reload_web') as reload,redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['web','config','set','--port','8740']),0)
                self.assertEqual(read(path).port,8740)
            reload.assert_called_once_with(30)

    def test_password_reset_writes_only_private_password_and_reloads_only_web(self):
        from wxbridge.host import system
        account=SimpleNamespace(pw_uid=912,pw_gid=913)
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp') as directory:
            settings=Path(directory)/'web.json';password=Path(directory)/'web-password'
            with patch.object(cli,'WEB_CONFIG',settings),patch.object(cli,'WEB_PASSWORD',password), \
                    patch.object(cli,'need_root'),patch.object(install,'password_input',return_value='synthetic-password'), \
                    patch.object(system,'service_account',return_value=account),patch.object(system,'atomic_file') as write, \
                    patch.object(cli,'reload_web') as reload,patch.object(cli,'system_service') as service, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['web','password','reset']),0)
            write.assert_called_once_with(password,b'synthetic-password\n',uid=912,gid=913)
            reload.assert_called_once_with(30);service.assert_not_called()
            self.assertNotIn('synthetic-password',output.getvalue())

    def test_password_is_never_accepted_as_a_command_argument(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli.parser().parse_args(['web','password','reset','--password','synthetic-password'])

    def test_web_settings_owner_mode_and_symlink_are_checked(self):
        from wxbridge.host import settings
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp') as directory:
            path=Path(directory)/'web.json'
            value=dict(host='127.0.0.1',port=8730)
            original_stat=Path.stat
            def mode(uid,permissions):
                def info(current,*args,**kwargs):
                    if current==path:return SimpleNamespace(st_uid=uid,st_mode=permissions)
                    return original_stat(current,*args,**kwargs)
                return info
            with patch.object(settings,'private_json',return_value=value),patch.object(Path,'is_symlink',return_value=False), \
                    patch.object(settings,'os',SimpleNamespace(name='posix')):
                for uid,permissions in ((0,0o644),(995,0o600)):
                    with self.subTest(uid=uid,mode=permissions),patch.object(Path,'stat',mode(uid,permissions)),self.assertRaisesRegex(ValueError,'unsafe'):
                        settings.read_web(path)
                with patch.object(Path,'stat',mode(0,0o600)):
                    self.assertEqual(settings.web_document(settings.read_web(path)),value)


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
        names = ['config.json','src/provider/.env','instance/a/config.json','src/web/frontend/node_modules/lib.js',
                 'src/provider/build/old.py','src/provider/__pycache__/old.pyc','src/provider/models.json',
                 'src/web/frontend/dist/index.html','src/provider/client.py','.env.example']
        for name in names:
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('synthetic')
        values={p.as_posix() for p in install.public_files(source)}
        self.assertIn('src/web/frontend/dist/index.html',values)
        self.assertIn('src/provider/client.py',values)
        self.assertIn('.env.example',values)
        self.assertEqual(values.intersection(names),{'src/web/frontend/dist/index.html','src/provider/client.py','.env.example'})

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
        self.assertIn('-m wxbridge --root /opt/wxbridge',service)
        self.assertNotIn('--web-host',service)
        self.assertIn('TimeoutStopSec=infinity',service)
        self.assertIn('SendSIGKILL=no',service)

    def test_new_installation_is_paused_and_has_no_bound_database_or_models(self):
        document=install.initial_config(Paths())
        from wxbridge.config import default_document
        from wxbridge.config.codec import account_from_document
        from wxbridge.config_merge import deep_merge
        cfg=account_from_document(deep_merge(default_document(),document))
        self.assertEqual(cfg.db_dir,'')
        self.assertEqual(cfg.helper_transport.allowed_chat_ids,('filehelper',))
        self.assertNotIn('models',document)
        self.assertNotIn('instances',document)
        self.assertEqual(Path(cfg.memory_directory),Paths().home/'memory')

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
        frontend=self.root/'src/web/frontend/dist/index.html'
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
    def test_concurrent_web_save_waits_for_binding_and_rollback(self):
        from threading import Event,Thread
        from web.backend.demo import seed
        from wxbridge.management.repository import ConfigRepository
        from wxbridge.management.settings import account_settings,save_account_settings
        seed(self.paths.app);repo=ConfigRepository(self.paths.app)
        for fail in (False,True):
            with self.subTest(rollback=fail):
                started,done=Event(),Event();errors=[];threads=[];blocked=[]
                target=self.paths.app/'config.json'
                helper=self.paths.helper/'helper-config.json'
                def save():
                    started.set()
                    try:
                        current=account_settings(repo)
                        save_account_settings(repo,{'poll_interval_seconds':9},current['revision'])
                    except Exception as exc:errors.append(exc)
                    finally:done.set()
                def write(path,value,**kwargs):
                    if path==target and not threads:
                        thread=Thread(target=save);threads.append(thread);thread.start()
                        started.wait(2);blocked.append(not done.wait(.05))
                    if path==helper and fail:raise ValueError('synthetic_binding_failure')
                    Path(path).write_text(json.dumps(value),encoding='utf-8')
                try:
                    with self.binding_patches(),patch.object(setup,'atomic_json',side_effect=write):
                        if fail:
                            with self.assertRaisesRegex(ValueError,'synthetic_binding_failure'):
                                setup.bind_account(self.paths,'/synthetic/db_storage')
                        else:setup.bind_account(self.paths,'/synthetic/db_storage')
                finally:
                    for thread in threads:thread.join(5)
                self.assertEqual(blocked,[True]);self.assertTrue(done.is_set());self.assertEqual(errors,[])
                self.assertEqual(account_settings(repo)['values']['poll_interval_seconds'],9)

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
        with self.binding_patches(), patch.object(setup,'create_bound_instance') as child:
            result=setup.bind_account(self.paths,'/synthetic/db_storage')
        self.assertTrue(result['bound']);self.assertFalse(result['bot_started'])
        self.assertEqual(json.loads((self.paths.app/'config.json').read_text())['db_dir'],str(self.paths.home/'account/db_storage'))
        self.assertEqual(private.read_text(),'SYNTHETIC_PRIVATE_VALUE')
        child.assert_not_called()

    def test_failed_instance_creation_restores_all_binding_and_allowlist_files(self):
        paths=[self.paths.app/'config.json',self.paths.helper/'helper-config.json',self.paths.settings]
        before={p:p.read_bytes() for p in paths}
        with self.binding_patches(), patch.object(setup,'create_bound_instance',side_effect=ValueError('synthetic_failure')):
            with self.assertRaisesRegex(ValueError,'回退'):
                setup.bind_account(self.paths,'/synthetic/db_storage',chat='12345@chatroom',identifier='alpha')
        self.assertEqual({p:p.read_bytes() for p in paths},before)

    def test_created_instance_is_disabled_but_ready_for_explicit_model_selection(self):
        def child(paths,identifier,template,account):
            directory=self.paths.app/'instance/alpha';directory.mkdir(parents=True)
            (directory/'config.json').write_text(json.dumps(template))
            return SimpleNamespace(returncode=0)
        with self.binding_patches(), patch.object(setup,'create_bound_instance',side_effect=child), patch.object(setup,'restart_sender'):
            result=setup.bind_account(self.paths,'/synthetic/db_storage',chat='12345@chatroom',identifier='alpha')
        value=json.loads((self.paths.app/'instance/alpha/config.json').read_text())
        self.assertTrue(result['instance_created']);self.assertFalse(value['enabled'])
        self.assertTrue(value['participation']['enabled'])
        self.assertFalse(value['search']['enabled']);self.assertFalse(value['image_generation']['enabled'])


if __name__=='__main__':
    unittest.main()
