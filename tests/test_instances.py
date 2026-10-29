"""Synthetic multi-group isolation and account scheduling; no live credentials."""
from model_support import default_config
from contextlib import nullcontext
from contextlib import nullcontext
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from synthetic import NOW, msg
from test_context_pipeline import FakeJob
from wxbridge.ai.roles import model_settings
from wxbridge.app.coordinator import Coordinator
from wxbridge.app.native_gateway import AccountNativeService
from wxbridge.config import load_config
from wxbridge.instances import enabled_instances, runtime_locks
from wechat_linux.errors import NativeError

CHATS = ('10001@chatroom', '10002@chatroom')


class InstanceFixture(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'config.json'
        self.data = dict(queue=dict(file=str(self.root/'queue.sqlite3')),
                         state_file=str(self.root/'state.json'), memory=dict(directory=str(self.root/'memory')),
                         provider_env=str(self.root/'provider.env'))
        models = default_config()['models']
        self.instance_data = [dict(id=name, chat=chat, enabled=True,
            system_prompt_file='system.md', models=deepcopy(models),
            target=dict(bot_name=name), search=dict(enabled=False),
            memory=dict(enabled=False, avatar=dict(enabled=False)))
            for name,chat in zip(('alpha','beta'),CHATS)]
        p = patch.dict(os.environ, {}, clear=True); p.start(); self.addCleanup(p.stop)

    def load(self, data=None):
        from wxbridge.instances import GROUP_FIELDS
        from wxbridge.config_merge import deep_merge
        root = deepcopy(self.data if data is None else data)
        common = {key:root.pop(key) for key in list(root) if key in GROUP_FIELDS and key not in ('queue','memory')}
        for section,key in (('queue','file'),('memory','directory')):
            value=root.get(section,{})
            common[section]={k:v for k,v in value.items() if k!=key}
            root[section]={key:value[key]} if key in value else {}
        self.path.write_text(json.dumps(root),encoding='utf-8')
        for entry in self.instance_data:
            directory=self.root/'instance'/entry['id']
            directory.mkdir(parents=True,exist_ok=True)
            body=deep_merge(common,{k:v for k,v in entry.items() if k!='id'})
            (directory/'config.json').write_text(json.dumps(body),encoding='utf-8')
            (directory/'system.md').write_text(entry['id']+' instructions',encoding='utf-8')
        return load_config(self.path)


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


class MultiGroupTests(InstanceFixture):
    def setUp(self):
        super().setUp()
        self.data.update(memory=dict(enabled=False, directory=str(self.root/'memory')),
                         native_api=dict(enabled=True, allowed_chat_ids=list(CHATS)), ui=dict(backend='native_service', dry_run=False),
                         participation=dict(enabled=True, debounce_seconds=0), target=dict(cooldown_seconds=0),
                         queue=dict(file=str(self.root/'queue.sqlite3')),
                         account=dict(send_interval_seconds=0))
        self.cfg = self.load()
        self.cfg['poll_interval_seconds'] = 0
        for e in self.cfg['instances']:
            e['config']['poll_interval_seconds'] = 0
            Path(e['config']['queue']['file']).parent.mkdir(parents=True, mode=0o700)
        self.rows, self.now, self.jobs, self.decision_jobs = {chat: [] for chat in CHATS}, float(NOW), [], []
        self.store = Mock()
        self.store.self_wxid = 'wxid_bot'
        self.store.resolve_chat.side_effect = lambda chat: (CHATS[0] if chat == '旧群' else chat, '合成群')
        self.store.last_local_id.side_effect = lambda chat: max([m['local_id'] for m in self.rows[chat]], default=0)
        self.store.messages_after.side_effect = lambda chat, cursor, limit: [m for m in self.rows[chat] if m['local_id'] > cursor][:limit]
        self.store.messages_in_window.side_effect = lambda chat, start, end: [m for m in self.rows[chat] if start <= m['time'] <= end]
        for target, opts in [('wxbridge.storage.journal.checked_path', dict(side_effect=lambda v, **kw: Path(v))),
                             ('wxbridge.app.bot.time.time', dict(side_effect=lambda: self.now)),
                             ('wxbridge.native.server.NativeServer', {})]:
            p = patch(target, **opts); p.start(); self.addCleanup(p.stop)
        self.coordinator = self.make()
        self.addCleanup(lambda: self.coordinator.close())

    def make(self):
        coordinator = Coordinator(self.cfg, store=self.store)
        for session in coordinator.sessions:
            def factory(kind, payload, _id=session.instance_id, _chat=session.chat_id, **kwargs):
                job = FakeJob(kind, payload, **kwargs)
                job.owner = _id
                if kind == 'participation':
                    source_ids = payload[2]['source_ids']
                    rows = {m['local_id']: m for m in self.rows[_chat]}
                    selected = [identifier for identifier in source_ids if
                        self.store.self_wxid in (rows[identifier].get('mentioned_users') or [])
                        or (rows[identifier].get('pat') or {}).get('target') == self.store.self_wxid]
                    job.result = dict(ok=True, decision=dict(reply_to=selected))
                    self.decision_jobs.append(job)
                else:
                    self.jobs.append(job)
                return job
            session.job_factory = factory
        return coordinator

    def add(self, index, identifier, text='合成提问', sender='wxid_friend', mention=True):
        row = msg(identifier, int(self.now), text, sender)
        row['mentioned_users'] = ['wxid_bot'] if mention else []
        self.rows[CHATS[index]].append(row)
        return row

    def tick(self): self.coordinator.tick()

    def active(self, kind): return [j for j in self.jobs if j.kind == kind and not j.closed]

    def finish(self, owner, reply='合成回复'):
        for job in self.active('generate'):
            if job.owner == owner: job.result = dict(ok=True, reply=reply)
        self.tick()

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
        a.memory_job = (record, {}, worker)
        self.add(1, 1); b._poll_messages()
        b.participate()
        self.assertFalse(worker.closed)
        self.assertIsNotNone(a.memory_job)
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
        from wxbridge.cli import main
        from wxbridge.app.runtime import run_bot
        from contextlib import nullcontext
        cfg = deepcopy(self.cfg)
        with patch('wxbridge.cli.load_config', return_value=cfg), patch('wxbridge.cli.memory_command', return_value=0) as memory:
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
        a._poll_generation_results()
        # The other group's message arrives after polling but before submission.
        self.add(1, 1)
        a._send_step()
        self.assertEqual(b.last_local_id, 1)
        self.assertEqual(len(self.active('native_send')), 1)
        self.assertEqual(self.active('native_send')[0].owner, 'alpha')
        self.assertFalse(b.journal.tasks())



if __name__ == '__main__':
    unittest.main()
