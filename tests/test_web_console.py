"""Synthetic management data only; never reads an account database or contacts a model."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from web.backend.demo import DEMO_PASSWORD, DemoServices, seed
from wxbridge.management.repository import ConfigRepository, ManagementError
from web.backend.services import BOT, RECOVERY, SESSION, ServiceManager, Operations
from web.backend.status import overview


class RepositoryTests(unittest.TestCase):
    def test_contact_search_normalizes_and_tolerates_mistyped_names(self):
        from wxbridge.management.contacts import match_score
        self.assertGreater(match_score('开 发', ['开发讨论群']),0)
        self.assertGreater(match_score('ＡＬＩＣＥ',['Alice']),0)
        self.assertGreater(match_score('alce',['Alice']),0)
        self.assertEqual(match_score('zzzz',['Alice']),-1)

    def test_web_schema_covers_business_fields_and_uses_runtime_constraints(self):
        from wxbridge.management.settings import instance_schema,GROUPS
        from wxbridge.config.models import InstanceConfig
        from wxbridge.config.codec import json_value
        schema=instance_schema()
        def leaves(value,prefix):
            for key,item in value.items():
                path=prefix+'.'+key
                if isinstance(item,dict):yield from leaves(item,path)
                else:yield path
        expected={p for _,_,roots,_ in GROUPS for name in roots for p in leaves(json_value(getattr(InstanceConfig(),name)),name)}-{'memory.system_prompt_file'}
        actual={field['path']:field for group in schema['groups'] for field in group['fields']}
        self.assertEqual(set(actual),expected)
        self.assertEqual(actual['vision.max_images']['max'],4)
        self.assertFalse(actual['vision.context_images_enabled']['default'])
        self.assertEqual(actual['vision.context_images_enabled']['type'],'boolean')
        self.assertEqual(actual['target.context_max_chars']['default'],15000)
        self.assertNotIn('provider_env',actual)
        self.assertNotIn('memory.directory',actual)

    def test_account_setting_update_preserves_paths_instances_and_credentials(self):
        from wxbridge.management.settings import account_settings,save_account_settings
        before=json.loads((self.root/'config.json').read_text())
        instance=(self.root/'instance/private_demo/config.json').read_bytes()
        provider=(self.root/'provider.env').read_bytes()
        current=account_settings(self.repository)
        saved=save_account_settings(self.repository,{'poll_interval_seconds':3.5,'account.send_interval_seconds':2,'ui.dry_run':True},current['revision'])
        self.assertEqual(saved['activation'],'account_apply_required')
        after=json.loads((self.root/'config.json').read_text())
        self.assertEqual(after['provider_env'],before['provider_env'])
        self.assertEqual(after['queue'],before['queue'])
        self.assertEqual((self.root/'instance/private_demo/config.json').read_bytes(),instance)
        self.assertEqual((self.root/'provider.env').read_bytes(),provider)
        with self.assertRaises(ManagementError) as stale:
            save_account_settings(self.repository,{'poll_interval_seconds':4},current['revision'])
        self.assertEqual(stale.exception.status,409)

    def test_account_setting_rejects_resource_overrides_and_invalid_values(self):
        from wxbridge.management.settings import account_settings,save_account_settings
        current=account_settings(self.repository)
        for values in ({'db_dir':'/tmp'}, {'native_api.allowed_chat_ids':['wxid_other']}, {'provider_env':'/tmp/private'},
                       {'poll_interval_seconds':0},{'poll_interval_seconds':True},{'ui.dry_run':'false'},
                       {'account.send_interval_seconds':float('nan')},{'ui.chat_open_wait':1.5}):
            with self.assertRaises(ManagementError):save_account_settings(self.repository,values,current['revision'])
        self.assertEqual(account_settings(self.repository)['revision'],current['revision'])

    def test_account_send_interval_validation_precedes_write(self):
        from wxbridge.management.settings import ACCOUNT_FIELDS, account_settings, instance_schema, save_account_settings
        field=next(item for item in instance_schema()['account_fields'] if item['path']=='account.send_interval_seconds')
        self.assertEqual(field['max'],60)
        for value in (0,60):
            current=account_settings(self.repository)
            saved=save_account_settings(self.repository,{'account.send_interval_seconds':value},current['revision'])
            self.assertEqual(saved['values']['account.send_interval_seconds'],value)
        before=(self.root/'config.json').read_bytes()
        current=account_settings(self.repository)
        # Runtime validation must still protect the file if the UI schema changes.
        fields=[{k:v for k,v in item.items() if k!='max'} for item in ACCOUNT_FIELDS]
        with patch('wxbridge.management.settings.ACCOUNT_FIELDS',fields):
            with self.assertRaises(ManagementError) as error:
                save_account_settings(self.repository,{'account.send_interval_seconds':60.1},current['revision'])
        self.assertEqual(error.exception.code,'invalid_account_settings')
        self.assertEqual((self.root/'config.json').read_bytes(),before)
        self.assertEqual(account_settings(self.repository)['revision'],current['revision'])

    def test_read_only_account_directory_reports_a_clear_error(self):
        from wxbridge.management.settings import account_settings,save_account_settings
        current=account_settings(self.repository)
        with patch('wxbridge.management.settings.os.access',return_value=False):
            self.assertFalse(account_settings(self.repository)['editable'])
            with self.assertRaises(ManagementError) as error:
                save_account_settings(self.repository,{'poll_interval_seconds':4},current['revision'])
        self.assertEqual(error.exception.status,403)
        self.assertEqual(account_settings(self.repository)['revision'],current['revision'])

    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        seed(self.root)
        self.repository=ConfigRepository(self.root)

    def item(self):
        return self.repository.get('private_demo')

    def test_management_lock_does_not_require_a_writable_application_code_directory(self):
        self.root.chmod(0o555)
        try:
            repository=ConfigRepository(self.root)
            self.assertEqual(repository.lock.path,self.root/'instance/.management.lock')
            self.assertEqual(repository.get('private_demo')['id'],'private_demo')
        finally:
            self.root.chmod(0o755)

    def test_process_lock_covers_revision_check_and_supports_nested_repository_calls(self):
        item=self.item()
        source=Path(__file__).resolve().parents[1]/'src'
        code='''
import json,sys
sys.path.insert(0,sys.argv[1])
from wxbridge.management.repository import ConfigRepository,ManagementError
repository=ConfigRepository(sys.argv[2])
print('waiting',flush=True)
try:
 repository.save('private_demo',json.loads(sys.argv[3]),sys.argv[4])
except ManagementError as exc:
 print(exc.code,flush=True)
else:
 raise SystemExit('stale write was accepted')
'''
        with self.repository.lock:
            child=subprocess.Popen([sys.executable,'-I','-c',code,str(source),str(self.root),
                                    json.dumps(item['config']),item['revision']],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'waiting')
                with self.assertRaises(subprocess.TimeoutExpired):child.communicate(timeout=.2)
                changed=dict(item['config'],enabled=False)
                self.repository.save('private_demo',changed,item['revision'])
            except BaseException:
                child.kill();child.communicate();raise
        try:
            output,error=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,error)
            self.assertIn('revision_conflict',output)
            self.assertFalse(self.item()['config']['enabled'])
        finally:
            if child.poll() is None:child.kill();child.communicate()

    def test_edit_preserves_unknown_business_settings_and_does_not_open_database(self):
        item=self.item()
        item['config']['search']={'enabled':False}
        with patch('sqlite3.connect',side_effect=AssertionError('no database')):
            result=self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(result['config']['search'],{'enabled':False})
        self.assertEqual(result['activation'],'restart_required')
        self.assertEqual(result['config']['chat'],item['config']['chat'])

    def test_legacy_tool_rounds_are_editable_without_writing_until_save(self):
        path=self.root/'instance/private_demo/config.json'
        original=json.loads(path.read_text(encoding='utf-8'))
        original['search']={'enabled':False,'max_tool_rounds':3}
        path.write_text(json.dumps(original),encoding='utf-8')
        raw=path.read_bytes()
        item=self.item()
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(item['config']['tools']['max_rounds'],3)
        self.assertNotIn('max_tool_rounds',item['config']['search'])
        self.assertEqual(item['effective']['tools']['max_rounds'],3)
        saved=self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(saved['config']['tools']['max_rounds'],3)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['search'],{'enabled':False})
        changed=deepcopy(saved['config'])
        changed['tools']['max_rounds']=1
        saved=self.repository.save('private_demo',changed,saved['revision'])
        self.assertEqual(saved['effective']['tools']['max_rounds'],1)
        reset=deepcopy(saved['config'])
        del reset['tools']['max_rounds']
        saved=self.repository.save('private_demo',reset,saved['revision'])
        self.assertEqual(saved['effective']['tools']['max_rounds'],2)
        self.assertNotIn('max_tool_rounds',json.loads(path.read_text(encoding='utf-8'))['search'])

    def test_new_tool_rounds_win_over_legacy_on_read_and_save(self):
        path=self.root/'instance/private_demo/config.json'
        document=json.loads(path.read_text(encoding='utf-8'))
        document['search']={'max_tool_rounds':3}
        document['tools']={'max_rounds':1}
        path.write_text(json.dumps(document),encoding='utf-8')
        item=self.item()
        self.assertEqual(item['config']['tools']['max_rounds'],1)
        self.assertEqual(item['effective']['tools']['max_rounds'],1)
        saved=self.repository.save('private_demo',document,item['revision'])
        self.assertEqual(saved['effective']['tools']['max_rounds'],1)
        self.assertNotIn('max_tool_rounds',json.loads(path.read_text(encoding='utf-8'))['search'])

    def test_new_instance_rejects_non_object_config(self):
        with self.assertRaises(ManagementError) as error:
            self.repository.save('another',[],None,create=True)
        self.assertEqual(error.exception.code,'invalid_instance_fields')

    def test_stale_config_revision_does_not_overwrite_external_edit(self):
        item=self.item(); path=self.root/'instance/private_demo/config.json'
        changed=dict(item['config'],enabled=False)
        path.write_text(json.dumps(changed),encoding='utf-8')
        with self.assertRaises(ManagementError) as error:
            self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(error.exception.status,409)
        self.assertFalse(json.loads(path.read_text())['enabled'])

    def test_invalid_model_does_not_write_config(self):
        item=self.item(); item['config']['models']['reply']['options']['timeout_seconds']=999
        with self.assertRaises(ManagementError):
            self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(self.item()['revision'],item['revision'])

    def test_image_config_is_editable_and_mixed_protocol_options_are_rejected(self):
        item = self.item()
        item['config']['image_generation'] = dict(enabled=True)
        item['config']['vision'] = dict(context_images_enabled=True)
        item['config']['models']['image'] = dict(model='gpt-image-1',
            options=dict(size='1024x1024', quality='low', timeout_seconds=180))
        with patch('provider.client.create_sdk_client', side_effect=AssertionError('configuration is offline')):
            saved = self.repository.save('private_demo', item['config'], item['revision'])
        self.assertEqual(saved['effective']['models']['image']['model'], 'gpt-image-1')
        self.assertTrue(saved['effective']['image_generation']['enabled'])
        self.assertTrue(saved['effective']['vision']['context_images_enabled'])
        wrong = deepcopy(saved['config'])
        wrong['models']['image']['options']['aspect_ratio'] = '1:1'
        with self.assertRaises(ManagementError) as error:
            self.repository.save('private_demo', wrong, saved['revision'])
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.item()['revision'], saved['revision'])

    def test_nonfinite_json_values_do_not_reach_disk(self):
        item=self.item();item['config']['target']={'cooldown_seconds':float('nan')}
        with self.assertRaises(ManagementError):
            self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(self.item()['revision'],item['revision'])

    def test_duplicate_chat_disabled_or_enabled_is_rejected(self):
        item=self.item()
        with self.assertRaises(ManagementError) as error:
            self.repository.save('another',dict(item['config'],enabled=False),None,create=True)
        self.assertEqual(error.exception.code,'duplicate_instance')

    def test_private_cannot_escape_into_other_instance_prompt(self):
        item=self.item(); item['config']['system_prompt_file']='../group_demo/system.md'
        with self.assertRaises(ManagementError):
            self.repository.save('private_demo',item['config'],item['revision'])
        with self.assertRaises(ManagementError):
            self.repository.get('../..')

    def test_no_credentials_can_be_read_or_written_through_config(self):
        item=self.item();item['config']['models']['reply']['options']['api_key']='synthetic-private-value'
        with self.assertRaises(ManagementError):
            self.repository.save('private_demo',item['config'],item['revision'])
        self.assertNotIn('synthetic-private-value',json.dumps(self.repository.list()))

    def test_prompt_history_restore_and_stale_revision(self):
        original=self.repository.prompt('private_demo')
        current=self.repository.save_prompt('private_demo','新的简短提示词',original['revision'],original['config_revision'])
        self.assertEqual(current['activation'],'next_request')
        with self.assertRaises(ManagementError):
            self.repository.save_prompt('private_demo','过期修改',original['revision'],original['config_revision'])
        restored=self.repository.restore_prompt('private_demo',current['history'][0]['id'],current['revision'],current['config_revision'])
        self.assertEqual(restored['text'],original['text'])
        self.assertEqual(len(restored['history']),2)

    def test_new_prompt_binding_requires_restart(self):
        item=self.item();item['config'].pop('system_prompt_file')
        self.repository.save('private_demo',item['config'],item['revision'])
        current=self.repository.prompt('private_demo')
        result=self.repository.save_prompt('private_demo','独立提示词',current['revision'],current['config_revision'])
        self.assertEqual(result['activation'],'restart_required')
        self.assertTrue(result['configured'])

    def test_prompt_revision_also_protects_configuration(self):
        prompt=self.repository.prompt('private_demo')
        item=self.item();item['config']['enabled']=False
        self.repository.save('private_demo',item['config'],item['revision'])
        with self.assertRaises(ManagementError):
            self.repository.save_prompt('private_demo','不能覆盖',prompt['revision'],prompt['config_revision'])

    def test_profile_file_and_credentials_are_preserved(self):
        profile=self.root/'instance/private_demo/profile.json';profile.write_text('{"summary":"合成资料"}',encoding='utf-8')
        private=self.root/'provider.env';private.write_text('SYNTHETIC_PRIVATE=keep\n',encoding='utf-8')
        before=(profile.read_bytes(),private.read_bytes())
        prompt=self.repository.prompt('private_demo')
        self.repository.save_prompt('private_demo','简短回复',prompt['revision'],prompt['config_revision'])
        self.assertEqual(before,(profile.read_bytes(),private.read_bytes()))

    def test_old_snapshot_does_not_look_running_after_service_stop(self):
        services=DemoServices(self.root);operations=Operations(services,self.repository)
        self.addCleanup(operations.close)
        services.perform('start')
        first=overview(self.repository,services,operations,demo=True)
        self.assertEqual(first['runtime']['state'],'listening')
        services.perform('stop')
        second=overview(self.repository,services,operations,demo=True)
        self.assertEqual(second['runtime']['state'],'stopped')
        self.assertFalse(second['runtime']['snapshot_current'])

    def test_fresh_heartbeat_with_control_failure_is_not_reported_as_expired(self):
        services=DemoServices(self.root);services.perform('start')
        services.status()
        services.status_error='control_permission_denied'
        operations=Operations(services,self.repository);self.addCleanup(operations.close)
        from wxbridge.control import COMPONENTS
        with patch.object(services,'status',return_value={n:dict(state='unavailable',pid=0) for n in COMPONENTS}):
            value=overview(self.repository,services,operations)
        self.assertEqual(value['runtime']['state'],'unavailable')
        self.assertEqual(value['runtime']['control_error'],'control_permission_denied')
        self.assertLess(value['runtime']['heartbeat_age_seconds'],10)


class ServiceTests(unittest.TestCase):
    def service(self):
        service=ServiceManager();service.allow_control=True
        return service

    def test_stop_is_one_fixed_supervisor_action(self):
        service=self.service()
        with patch('web.backend.services.request',return_value=dict(applied=True)) as call:
            service.perform('stop')
        call.assert_called_once_with('stop',service.control_socket)

    def test_apply_never_starts_deliberately_stopped_bot(self):
        service=self.service()
        with patch('web.backend.services.request',return_value=dict(applied=False)) as call:
            self.assertFalse(service.perform('apply')['applied'])
        call.assert_called_once_with('apply',service.control_socket)

    def test_supervisor_unavailable_is_reported_without_shell_fallback(self):
        service=self.service()
        with patch('web.backend.services.request',side_effect=OSError):
            with self.assertRaisesRegex(ManagementError,'主控暂时不可用'):service.perform('apply')

    def test_arbitrary_unit_or_action_is_never_executed(self):
        service=self.service()
        with patch('web.backend.services.request') as call:
            with self.assertRaises(ManagementError):service.perform('stop sshd.service')
        call.assert_not_called()

    def test_status_preserves_control_failure_category_and_clears_it_after_recovery(self):
        service=self.service()
        for error,code in [(PermissionError(),'control_permission_denied'),
                           (FileNotFoundError(),'control_unreachable'),
                           (TimeoutError(),'control_timeout'),(ValueError(),'control_invalid_response')]:
            with self.subTest(code=code),patch('web.backend.services.os.name','posix'), \
                    patch('web.backend.services.request',side_effect=error):
                self.assertEqual(service.status()[BOT]['state'],'unavailable')
                self.assertEqual(service.status_error,code)
        with patch('web.backend.services.os.name','posix'),patch('web.backend.services.request',return_value=dict(components={})):
            service.status()
        self.assertEqual(service.status_error,'')
