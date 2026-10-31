"""Synthetic multi-group isolation and account scheduling; no live credentials."""
from tests.support.model_support import default_config
from contextlib import nullcontext
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.support.synthetic import NOW, msg
from tests.support.pipeline import FakeJob
from wxbridge.ai.roles import model_settings
from wxbridge.app.coordinator import Coordinator
from wxbridge.app.native_gateway import AccountNativeService
from wxbridge.config import load_config
from wxbridge.instances import enabled_instances, runtime_locks
from wechat_linux.errors import NativeError
from tests.support.instances import CHATS, InstanceFixture, MultiGroupFixture


class InstanceConfigTests(InstanceFixture):
    def test_directory_configs_are_independent(self):
        self.instance_data[0]['models']['reply']['model']='alpha-model'
        self.instance_data[1]['models']['reply']['model']='beta-model'
        cfg=self.load()
        a,b=[c for _,c in enabled_instances(cfg)]
        self.assertEqual(model_settings(a)['model'],'alpha-model')
        self.assertEqual(model_settings(b)['model'],'beta-model')
        self.assertNotEqual(a['queue']['file'],b['queue']['file'])
        self.assertEqual(a['system_prompt_file'],str((self.root/'instance/alpha/system.md').resolve()))
        a['models']['reply']['model']='changed'
        self.assertEqual(cfg['instances'][0]['config']['models']['reply']['model'],'alpha-model')

    def test_disabled_unconfigured_instance_is_allowed(self):
        self.instance_data[0].update(enabled=False,chat='')
        self.instance_data[0]['models']['reply']['model']=''
        cfg=self.load()
        self.assertFalse(cfg['instances'][0]['enabled'])
        self.assertEqual([name for name,_ in enabled_instances(cfg)],['beta'])

    def test_root_group_configuration_is_rejected(self):
        self.path.write_text(json.dumps(dict(instances=[])),encoding='utf-8')
        with self.assertRaises(ValueError): load_config(self.path)

    def test_duplicate_chat_and_account_overrides_are_rejected(self):
        self.instance_data[1]['chat']=CHATS[0]
        with self.assertRaises(ValueError): self.load()
        self.instance_data[1]['chat']=CHATS[1]
        self.instance_data[1]['db_dir']='/other'
        with self.assertRaises(ValueError): self.load()

    def test_enabled_instance_requires_a_model(self):
        self.instance_data[0]['models']['reply']['model']=''
        with self.assertRaisesRegex(ValueError,'missing_model'): self.load()


class MultiGroupTests(MultiGroupFixture):


    def test_one_reader_two_independent_cursors_contexts_and_model_slots(self):
        self.add(0, 1, 'ONLY_ALPHA'); self.add(0, 2, 'ALPHA_SECOND'); self.add(1, 1, 'ONLY_BETA')
        self.tick()
        sessions = self.coordinator.sessions
        self.assertIs(sessions[0].store, sessions[1].store)
        self.assertEqual([s.last_local_id for s in sessions], [2, 1])
        self.assertEqual({j.owner for j in self.active('generate')}, {'alpha', 'beta'})
        self.assertEqual(self.coordinator.account.occupied(), 2)
        for job in self.active('generate'):
            rendered = json.dumps(job.payload[1], ensure_ascii=False)
            self.assertIn('alpha instructions' if job.owner == 'alpha' else 'beta instructions', rendered)
            self.assertNotIn('ONLY_BETA' if job.owner == 'alpha' else 'ONLY_ALPHA', rendered)
        self.add(1, 2, '新问题'); self.tick()
        self.assertEqual(sessions[1].last_local_id, 2)
        self.assertEqual(len(self.active('generate')), 3)

    def test_slow_group_does_not_limit_another_groups_intake_or_generation(self):
        self.add(0, 1); self.add(0, 2); self.add(1, 1); self.tick()
        self.assertEqual(self.active('generate')[0].owner, 'alpha')
        self.add(1, 2); self.tick()
        self.assertEqual(self.coordinator.sessions[1].last_local_id, 2)
        self.finish('alpha')
        self.assertEqual(len(self.active('generate')), 2)
        self.assertEqual({j.owner for j in self.active('generate')}, {'beta'})
        self.assertIsNone(self.coordinator.account.status()['model_capacity'])

    def test_serial_sends_confirm_only_in_target_group_then_other_group_proceeds(self):
        self.add(0, 1); self.add(1, 1); self.tick()
        for job in self.active('generate'): job.result = dict(ok=True, reply='相同回复')
        self.tick()
        sends = self.active('native_send')
        self.assertEqual(len(sends), 1)
        owner = sends[0].owner
        index = 0 if owner == 'alpha' else 1
        self.assertEqual(sends[0].payload[1], CHATS[index])
        self.add(1-index, 2, '相同回复', sender='wxid_bot', mention=False)
        sends[0].result = dict(ok=True, ui={})
        self.tick()
        first = self.coordinator.sessions[index]
        self.assertIsNotNone(first.journal.head()['intent'])
        self.assertFalse(self.active('native_send'))
        self.add(index, 2, '相同回复', sender='wxid_bot', mention=False)
        self.tick()
        self.assertEqual(first.journal.status()['queue_length'], 0)
        self.assertEqual(len(self.active('native_send')), 1)
        self.assertNotEqual(self.active('native_send')[0].owner, owner)

    def test_unknown_send_survives_restart_and_never_resubmits(self):
        self.add(0, 1); self.tick(); self.finish('alpha')
        send = self.active('native_send')[0]
        send.result = dict(ok=False, ui=dict(error='native_worker_failed', operation=dict(submitted=None)))
        self.tick()
        old = self.coordinator.sessions[0].journal.head()['intent']['request_id']
        self.coordinator.close()
        self.coordinator = self.make()
        self.add(1, 1); self.tick(); self.finish('beta')
        self.now += 11; self.tick()
        self.assertFalse(self.active('native_send'))
        self.assertEqual(self.coordinator.sessions[0].journal.head()['intent']['request_id'], old)
        self.assertTrue(self.coordinator.account.status()['send_blocked'])
        self.assertEqual(self.coordinator.sessions[1].last_local_id, 1)

    def test_account_gap_and_per_group_cooldown_wait_without_dropping(self):
        self.coordinator.account.send_gap = 5
        self.add(0, 1); self.tick(); self.finish('alpha')
        self.active('native_send')[0].result = dict(ok=True, ui={})
        self.add(0, 2, sender='wxid_bot', text='合成回复', mention=False); self.tick()
        self.add(1, 1); self.tick(); self.finish('beta')
        self.assertFalse(self.active('native_send'))
        self.assertEqual(self.coordinator.sessions[1].journal.status()['queue_length'], 1)
        self.now += 5; self.tick()
        self.assertEqual(self.active('native_send')[0].owner, 'beta')

    def test_other_groups_foreground_does_not_cancel_running_memory(self):
        a, b = self.coordinator.sessions
        worker = FakeJob('memory', ())
        record = dict(id=1, state='running', triggers=[dict(local_id=1, time=NOW)], attempts=0, retry_at=0)
        with a.journal.db: a.journal.put_memory(record)
        a.memory.job = (record, {}, worker)
        self.add(1, 1); b._poll_messages()
        b.participate()
        self.assertFalse(worker.closed)
        self.assertIsNotNone(a.memory.job)
        self.assertEqual(a.journal.memory_jobs()[0]['state'], 'running')
        self.assertEqual(self.active('generate')[0].owner, 'beta')

    def test_external_request_ids_are_account_wide_and_routed_by_chat(self):
        service = AccountNativeService(self.coordinator, self.cfg['native_api'])
        value = dict(op='send_text', request_id='sample:one', chat_id=CHATS[1], text='合成外部请求')
        accepted = service.handle(value)
        self.assertEqual(self.coordinator.sessions[0].journal.tasks(), [])
        self.assertEqual(self.coordinator.sessions[1].journal.tasks()[0]['delivery_chat_id'], CHATS[1])
        self.assertTrue(service.handle(value)['duplicate'])
        self.assertEqual(service.handle(dict(op='status', request_id=value['request_id']))['task_id'], accepted['task_id'])
        with self.assertRaises(NativeError): service.handle(dict(value, chat_id=CHATS[0]))
        self.assertEqual(len(self.coordinator.sessions[1].journal.tasks()), 1)

    def test_rebind_instance_to_other_group_cannot_recover_old_queue(self):
        self.add(0, 1); self.tick()
        self.coordinator.close()
        changed = deepcopy(self.cfg)
        changed['instances'][0]['chat'] = CHATS[1]
        changed['instances'][0]['config']['target']['chat'] = CHATS[1]
        with self.assertRaisesRegex(ValueError, '队列的微信账号或目标群不匹配'):
            Coordinator(changed, store=self.store)
        self.coordinator = self.make()
        self.assertEqual(self.coordinator.sessions[0].last_local_id, 1)

    def test_account_status_contains_no_chat_bodies_prompts_keys_or_member_ids(self):
        self.add(0, 1, 'PRIVATE_CHAT'); self.tick()
        value = self.coordinator.status_path.read_text(encoding='utf-8')
        for secret in ('PRIVATE_CHAT', 'alpha instructions', 'wxid_friend', 'api_key', *CHATS):
            self.assertNotIn(secret, value)
        self.assertEqual(set(json.loads(value)['instances']), {'alpha', 'beta'})

    def test_account_status_keeps_each_instances_context_metrics_and_policy(self):
        first, second = self.coordinator.sessions
        first.cfg['target'].update(context_max_chars=7500,context_recent_messages=12)
        reply=dict(chars=1234,selected=30,retained=10,omitted=20,scanned=200,scan_limit_hit=False)
        participation=dict(chars=567,selected=5,retained=5,omitted=0,scanned=1000,scan_limit_hit=True)
        with first.journal.db:first.journal.set('context',dict(reply=reply))
        with second.journal.db:second.journal.set('context',dict(participation=participation))
        self.coordinator.save_status()
        rows=json.loads(self.coordinator.status_path.read_text())['instances']
        self.assertEqual(rows['alpha']['context'],dict(reply=reply))
        self.assertEqual(rows['beta']['context'],dict(participation=participation))
        self.assertEqual(rows['alpha']['context_policy']['context_max_chars'],7500)
        self.assertEqual(rows['alpha']['context_policy']['context_recent_messages'],12)
        self.assertEqual(rows['beta']['context_policy']['context_max_chars'],15000)


    def test_memory_for_same_member_in_different_groups_is_isolated(self):
        from wxbridge.storage.person_memory import MemoryStore
        a, b = self.coordinator.sessions
        root = self.root/'private-memory'
        first = MemoryStore(root, a.journal.get('identity'))
        second = MemoryStore(root, b.journal.get('identity'))
        self.assertNotEqual(first.path, second.path)
        first.data['members']['wxid_friend'] = dict(nickname='合成成员', entries=[], affinity=75)
        self.assertNotIn('wxid_friend', second.data['members'])


    def test_cli_selection_and_multi_run_share_one_owner(self):
        from wxbridge.debug_cli import main
        from wxbridge.app.runtime import run_bot
        from contextlib import nullcontext
        cfg = deepcopy(self.cfg)
        with patch('wxbridge.config.load_config', return_value=cfg), patch('wxbridge.debug_cli.memory_command', return_value=0) as memory:
            self.assertEqual(main(['--instance', 'beta', 'memory', 'status']), 0)
        self.assertEqual(memory.call_args.args[0]['target']['chat'], CHATS[1])
        with patch('wxbridge.app.runtime.instance_lock', side_effect=lambda v:nullcontext()) as locking, \
             patch('wxbridge.app.runtime.setup_logging'), \
             patch('wxbridge.app.coordinator.Coordinator') as make:
            run_bot(cfg, once=True)
            self.assertEqual(make.call_count, 1)
            self.assertEqual(len({c.args[0] for c in locking.call_args_list}), 4)
            make.return_value.close.assert_called_once()

    def test_disabled_instance_keeps_unknown_send_gate_and_request_history(self):
        service = AccountNativeService(self.coordinator, self.cfg['native_api'])
        value = dict(op='send_text', request_id='disabled:one', chat_id=CHATS[0], text='合成外部请求')
        service.handle(value)
        self.tick()
        self.assertEqual(len(self.active('native_send')), 1)
        self.coordinator.close()
        self.cfg['instances'][0]['enabled'] = False
        self.coordinator = self.make()
        self.assertTrue(self.coordinator.account.status()['send_blocked'])
        service = AccountNativeService(self.coordinator, self.cfg['native_api'])
        self.assertTrue(service.handle(value)['duplicate'])
        self.add(1, 1); self.tick(); self.finish('beta')
        self.assertFalse(self.active('native_send'))
        self.assertEqual(self.coordinator.sessions[0].last_local_id, 1)

    def test_pre_send_refresh_of_other_group_preserves_approved_delivery(self):
        a, b = self.coordinator.sessions
        self.add(0, 1)
        self.tick()
        self.active('generate')[0].result = dict(ok=True, reply='合成接话')
        a.replies.collect()
        # The other group's message arrives after polling but before submission.
        self.add(1, 1)
        a._send_step()
        self.assertEqual(b.last_local_id, 1)
        self.assertEqual(len(self.active('native_send')), 1)
        self.assertEqual(self.active('native_send')[0].owner, 'alpha')
        self.assertFalse(b.journal.tasks())


if __name__ == '__main__':
    unittest.main()
