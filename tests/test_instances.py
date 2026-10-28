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
                         instances=[dict(id='alpha', chat=CHATS[0], overrides=dict(
                             persona=dict(name='Alpha', aliases=['a酱']),
                             deepseek=dict(system_prompt='ALPHA_PERSONA'), search=dict(enabled=False))),
                                    dict(id='beta', chat=CHATS[1], overrides=dict(
                             persona=dict(name='Beta', aliases=['b酱']),
                             deepseek=dict(system_prompt='BETA_PERSONA'), search=dict(enabled=False)))])
        p = nullcontext(); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True); p.start(); self.addCleanup(p.stop)

    def load(self, data=None):
        self.path.write_text(json.dumps(self.data if data is None else data), encoding='utf-8')
        return load_config(self.path)


class InstanceConfigTests(InstanceFixture):

    def test_different_personas_profiles_and_paths_without_mutating_defaults(self):
        from wxbridge.config import DEFAULTS
        before = default_config()
        self.data['models'] = dict(reply=dict(profile='openai', model='old-root-model', proxy_url='http://127.0.0.1:10808'))
        self.data['instances'][1]['overrides']['models'] = dict(reply=dict(profile='deepseek_chat'))
        cfg = self.load()
        a, b = [c for _, c in enabled_instances(cfg)]
        self.assertEqual(a['target']['bot_name'], 'Alpha')
        self.assertEqual(b['target']['bot_name'], 'Beta')
        self.assertEqual(a['deepseek']['system_prompt'], 'ALPHA_PERSONA')
        self.assertEqual(model_settings(a)['model'], 'old-root-model')
        self.assertEqual(model_settings(b)['model'], 'deepseek-flash')
        self.assertIsNone(model_settings(b).get('proxy_url'))
        self.assertNotEqual(a['queue']['file'], b['queue']['file'])
        self.assertNotEqual(Path(a['state_file']).parent, Path(b['state_file']).parent)
        a['persona']['aliases'].append('独立')
        self.assertNotIn('独立', cfg['instances'][0]['config']['persona']['aliases'])
        self.assertEqual(DEFAULTS, before)

    def test_ids_chats_disabled_and_shared_resources_are_checked(self):
        bad = [dict(id='../oops'), dict(id='ALPHA'), dict(chat=CHATS[0]), dict(chat='显示群名'),
               dict(enabled=1), dict(reuse_legacy_state='yes'), dict(unknown=True),
               dict(overrides=dict(db_dir='/other')), dict(overrides=dict(queue=dict(file='/other'))),
               dict(overrides=dict(queue=dict(generation_concurrency=2))),
               dict(overrides=dict(memory=dict(directory='/other'))),
               dict(overrides=dict(target=dict(chat=CHATS[0]))),
               dict(overrides=dict(models=dict(reply=dict(model=''))))]
        for change in bad:
            with self.subTest(change=change):
                data = deepcopy(self.data); data['instances'][1].update(change)
                with self.assertRaises((ValueError, TypeError)):
                    self.load(data)
        data = deepcopy(self.data)
        for entry in data['instances']: entry['enabled'] = False
        with self.assertRaises(ValueError): self.load(data)
        with patch.dict(os.environ, WX_GROUP='other'):
            with self.assertRaisesRegex(ValueError, 'WX_GROUP'): self.load()

    def test_legacy_state_is_explicit_and_keeps_original_paths(self):
        self.data['target'] = dict(chat='旧群显示名')
        self.data['instances'][0]['reuse_legacy_state'] = True
        cfg = self.load()
        self.assertEqual(cfg['instances'][0]['config']['queue']['file'], cfg['queue']['file'])
        self.assertEqual(cfg['instances'][0]['config']['state_file'], cfg['state_file'])
        self.assertEqual(len(runtime_locks(cfg)), 3)
        self.data['instances'][1]['reuse_legacy_state'] = True
        with self.assertRaises(ValueError): self.load()

    def test_prompt_paths_are_relative_to_config_and_checked_per_instance(self):
        for entry in self.data['instances']:
            entry['overrides']['persona']['prompt_file'] = 'prompts/'+entry['id']+'.md'
        cfg = self.load()
        for entry in cfg['instances']:
            self.assertEqual(entry['config']['deepseek']['system_prompt_file'],
                             str((self.root/'prompts'/f"{entry['id']}.md").resolve()))

    def test_same_profile_keeps_model_override_and_other_roles_are_independent(self):
        self.data['models'] = dict(reply=dict(profile='openai', model='synthetic-sol', reasoning_effort='medium'))
        self.data['instances'][0]['overrides']['models'] = dict(reply=dict(profile='openai', reasoning_effort='high'),
            memory=dict(profile='deepseek_chat', thinking='enabled', reasoning_effort='max'))
        cfg = self.load()
        active = cfg['instances'][0]['config']
        self.assertEqual(model_settings(active)['model'], 'synthetic-sol')
        self.assertEqual(model_settings(active)['reasoning_effort'], 'high')
        self.assertEqual(model_settings(active, 'memory')['provider'], 'deepseek')
        self.assertEqual(model_settings(cfg)['reasoning_effort'], 'medium')


class MultiGroupTests(InstanceFixture):
    def setUp(self):
        super().setUp()
        self.data.update(memory=dict(enabled=False, directory=str(self.root/'memory')),
                         native_api=dict(enabled=True, allowed_chat_ids=list(CHATS)), ui=dict(backend='native_service', dry_run=False),
                         proactive=dict(enabled=False), target=dict(cooldown_seconds=0),
                         queue=dict(file=str(self.root/'queue.sqlite3'), debounce_seconds=0),
                         account=dict(send_interval_seconds=0))
        self.cfg = self.load()
        self.cfg['poll_interval_seconds'] = 0
        for e in self.cfg['instances']:
            e['config']['poll_interval_seconds'] = 0
            Path(e['config']['queue']['file']).parent.mkdir(parents=True, mode=0o700)
        self.rows, self.now, self.jobs = {chat: [] for chat in CHATS}, float(NOW), []
        self.store = Mock()
        self.store.self_wxid = 'wxid_bot'
        self.store.resolve_chat.side_effect = lambda chat: (CHATS[0] if chat == '旧群' else chat, '合成群')
        self.store.last_local_id.side_effect = lambda chat: max([m['local_id'] for m in self.rows[chat]], default=0)
        self.store.messages_after.side_effect = lambda chat, cursor, limit: [m for m in self.rows[chat] if m['local_id'] > cursor][:limit]
        self.store.messages_in_window.side_effect = lambda chat, start, end: [m for m in self.rows[chat] if start <= m['time'] <= end]
        for target, opts in [('wxbridge.storage.journal.checked_path', dict(side_effect=lambda v, **kw: Path(v))),
                             ('wxbridge.app.bot.time.time', dict(side_effect=lambda: self.now)),
                             ('wechat_linux.transport.server.NativeServer', {})]:
            p = patch(target, **opts); p.start(); self.addCleanup(p.stop)
        self.coordinator = self.make()
        self.addCleanup(lambda: self.coordinator.close())

    def make(self):
        coordinator = Coordinator(self.cfg, store=self.store)
        for session in coordinator.sessions:
            def factory(kind, payload, _id=session.instance_id, **kwargs):
                job = FakeJob(kind, payload, **kwargs)
                job.owner = _id
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
            self.assertIn('ALPHA_PERSONA' if job.owner == 'alpha' else 'BETA_PERSONA', rendered)
            self.assertNotIn('ONLY_BETA' if job.owner == 'alpha' else 'ONLY_ALPHA', rendered)
        self.add(1, 2, '新问题'); self.tick()
        self.assertEqual(sessions[1].last_local_id, 2)
        self.assertEqual(len(self.active('generate')), 2)

    def test_round_robin_with_one_slot_and_slow_request_does_not_block_intake(self):
        self.coordinator.account.capacity = 1
        self.add(0, 1); self.add(0, 2); self.add(1, 1); self.tick()
        self.assertEqual(self.active('generate')[0].owner, 'alpha')
        self.add(1, 2); self.tick()
        self.assertEqual(self.coordinator.sessions[1].last_local_id, 2)
        self.finish('alpha')
        # The scheduler starts at the group that did not receive the last slot.
        self.assertEqual(len(self.active('generate')), 1)
        self.assertEqual(self.active('generate')[0].owner, 'beta')

    def test_serial_sends_confirm_only_in_target_group_then_other_group_proceeds(self):
        self.add(0, 1); self.add(1, 1); self.tick()
        for job in self.active('generate'): job.result = dict(ok=True, reply='相同回复')
        self.tick()
        sends = self.active('native_send')
        self.assertEqual(len(sends), 1)
        owner = sends[0].owner
        index = 0 if owner == 'alpha' else 1
        self.assertEqual(sends[0].payload[3], CHATS[index])
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

    def test_memory_and_optional_work_yield_to_other_groups_foreground(self):
        a, b = self.coordinator.sessions
        worker = FakeJob('memory', ())
        record = dict(id=1, state='running', triggers=[dict(local_id=1, time=NOW)], attempts=0, retry_at=0)
        with a.journal.db: a.journal.put_memory(record)
        a.memory_job = (record, {}, worker)
        self.coordinator.account.capacity = 1
        self.add(1, 1); b._poll_messages()
        self.assertFalse(self.coordinator.account.can_start(a, 'proactive'))
        b._start_generations(max_new=1)
        self.assertTrue(worker.closed)
        self.assertIsNone(a.memory_job)
        self.assertEqual(a.journal.memory_jobs()[0]['state'], 'pending')
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
        for secret in ('PRIVATE_CHAT', 'ALPHA_PERSONA', 'wxid_friend', 'api_key', *CHATS):
            self.assertNotIn(secret, value)
        self.assertEqual(set(json.loads(value)['instances']), {'alpha', 'beta'})

    def test_explicit_legacy_adoption_keeps_cursor_and_pending_tasks(self):
        from wxbridge.storage.journal import Journal
        self.coordinator.close()
        legacy = Journal(self.cfg['queue']['file'], dict(account='wxid_bot', chat=CHATS[0]), 0)
        row = self.add(0, 1)
        legacy.intake([row], lambda m: True, self.now, 0)
        legacy.close()
        self.cfg['target']['chat'] = CHATS[0]
        entry = self.cfg['instances'][0]
        entry['reuse_legacy_state'] = True
        entry['config']['queue']['file'] = self.cfg['queue']['file']
        entry['config']['state_file'] = self.cfg['state_file']
        self.coordinator = self.make()
        a = self.coordinator.sessions[0]
        self.assertEqual(a.last_local_id, 1)
        self.assertEqual(a.journal.status()['queue_length'], 1)
        self.tick()
        self.assertEqual(len([j for j in self.active('generate') if j.owner == 'alpha']), 1)

    def test_memory_for_same_member_in_different_groups_is_isolated(self):
        from wxbridge.storage.person_memory import MemoryStore
        a, b = self.coordinator.sessions
        root = self.root/'private-memory'
        first = MemoryStore(root, a.journal.get('identity'))
        second = MemoryStore(root, b.journal.get('identity'))
        self.assertNotEqual(first.path, second.path)
        first.data['members']['wxid_friend'] = dict(nickname='合成成员', entries=[], affinity=75)
        self.assertNotIn('wxid_friend', second.data['members'])

    def test_background_memory_is_one_account_wide_and_addressed_judgment_is_not_discarded(self):
        a, b = self.coordinator.sessions
        a.memory_job = (dict(id=1, state='running', triggers=[dict(local_id=1, time=NOW)]), {}, FakeJob('memory', ()))
        self.assertFalse(self.coordinator.account.can_start(b, 'memory'))
        self.assertTrue(self.coordinator.account.can_start(b, 'proactive'))
        a._pause_memory()
        a.proactive.job = (FakeJob('proactive', ()), 1, self.now+30)
        a.proactive.pending = dict(reason='addressed')
        self.coordinator.account.capacity = 1
        self.assertFalse(self.coordinator.account.can_start(b, 'generate'))
        self.assertIsNotNone(a.proactive.job)

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

    def test_pre_send_refresh_of_other_group_cancels_optional_delivery(self):
        a, b = self.coordinator.sessions
        a.journal.enqueue_proactive('合成接话', 0, self.now, self.now+60, metadata={})
        # The message arrives after the normal poll but before submission.
        self.add(1, 1)
        a._send_step()
        self.assertEqual(b.last_local_id, 1)
        self.assertFalse(self.active('native_send'))
        self.assertEqual(b.journal.status()['queue_length'], 1)


if __name__ == '__main__':
    unittest.main()
