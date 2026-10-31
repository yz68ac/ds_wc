"""Synthetic management data only; never reads an account database or contacts a model."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from wxbridge_web.demo import DEMO_PASSWORD, DemoServices, seed
from wxbridge_web.repository import ConfigRepository, ManagementError
from wxbridge_web.services import BOT, RECOVERY, SESSION, ServiceManager, Operations
from wxbridge_web.status import overview


class RepositoryTests(unittest.TestCase):
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

    def test_edit_preserves_unknown_business_settings_and_does_not_open_database(self):
        item=self.item()
        item['config']['search']={'enabled':False}
        with patch('sqlite3.connect',side_effect=AssertionError('no database')):
            result=self.repository.save('private_demo',item['config'],item['revision'])
        self.assertEqual(result['config']['search'],{'enabled':False})
        self.assertEqual(result['activation'],'restart_required')
        self.assertEqual(result['config']['chat'],item['config']['chat'])

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
        item['config']['models']['image'] = dict(model='gpt-image-1',
            options=dict(size='1024x1024', quality='low', timeout_seconds=180))
        with patch('provider.client.create_sdk_client', side_effect=AssertionError('configuration is offline')):
            saved = self.repository.save('private_demo', item['config'], item['revision'])
        self.assertEqual(saved['effective']['models']['image']['model'], 'gpt-image-1')
        self.assertTrue(saved['effective']['image_generation']['enabled'])
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


class ServiceTests(unittest.TestCase):
    def service(self):
        service=ServiceManager();service.allow_control=True
        return service

    def test_stop_is_one_fixed_supervisor_action(self):
        service=self.service()
        with patch('wxbridge_web.services.request',return_value=dict(applied=True)) as call:
            service.perform('stop')
        call.assert_called_once_with('stop',service.control_socket)

    def test_apply_never_starts_deliberately_stopped_bot(self):
        service=self.service()
        with patch('wxbridge_web.services.request',return_value=dict(applied=False)) as call:
            self.assertFalse(service.perform('apply')['applied'])
        call.assert_called_once_with('apply',service.control_socket)

    def test_supervisor_unavailable_is_reported_without_shell_fallback(self):
        service=self.service()
        with patch('wxbridge_web.services.request',side_effect=OSError):
            with self.assertRaisesRegex(ManagementError,'主控暂时不可用'):service.perform('apply')

    def test_arbitrary_unit_or_action_is_never_executed(self):
        service=self.service()
        with patch('wxbridge_web.services.request') as call:
            with self.assertRaises(ManagementError):service.perform('stop sshd.service')
        call.assert_not_called()
