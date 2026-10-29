"""Synthetic group profiles: persistence, evidence binding and owner-loop lifecycle."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model_support import default_config
from synthetic import NOW, msg
from wxbridge.ai import group_profile as analysis
from wxbridge.app.group_profile import ProfileController, participant_ids
from wxbridge.storage.group_profile import GroupProfile


IDENTITY = dict(account='wxid_bot', chat='synthetic@chatroom')


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'profile.json'
        self.profile = GroupProfile(self.path, IDENTITY)
        self.profile.observe({'wxid_a': '同名', 'wxid_b': '同名'})

    def batch(self, number=1):
        return self.profile.analysis_snapshot([dict(id=f'm{number}', local_id=number,
            sender='wxid_a', text='合成证据', time=NOW)])

    def test_reload_keeps_distinct_wxids_with_identical_or_changed_aliases(self):
        self.profile.observe({'wxid_a': '新昵称'})
        loaded = GroupProfile(self.path, IDENTITY)
        self.assertEqual(loaded.snapshot(), dict(summary='', members={
            'wxid_a': dict(alias='新昵称'), 'wxid_b': dict(alias='同名')}))
        for identity in (dict(IDENTITY, account='other'), dict(IDENTITY, chat='other@chatroom')):
            with self.assertRaisesRegex(ValueError, 'profile_identity_mismatch'):
                GroupProfile(self.path, identity)

    def test_snapshot_is_detached_and_memory_disabled_does_not_delete_stored_data(self):
        self.profile.sync_memory({'wxid_a': [dict(summary='合成记忆')]})
        public = self.profile.snapshot()
        self.assertNotIn('memory', public['members']['wxid_a'])
        public['members']['wxid_a']['alias'] = '外部修改'
        private = self.profile.snapshot(memory_enabled=True)
        private['members']['wxid_a']['memory'][0]['summary'] = '修改'
        self.assertEqual(self.profile.data['members']['wxid_a'],
                         dict(alias='同名', memory=[dict(summary='合成记忆')]))
        self.assertEqual(set(self.profile.snapshot(members=['wxid_b'])['members']), {'wxid_b'})

    def test_proposals_cannot_change_aliases_members_or_use_unknown_sources(self):
        batch, before = self.batch(), self.path.read_bytes()
        proposals = [dict(summary='新概况', sources=[]), dict(summary='新概况', sources=['missing']),
            dict(summary='新概况', sources=['m1', 'm1']), dict(summary='新概况', sources=[True]),
            dict(summary='新概况', sources=['m1'], members={})]
        for proposal in proposals:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                self.profile.apply(proposal, batch)
            self.assertEqual(self.path.read_bytes(), before)

    def test_stale_result_and_other_group_are_discarded_but_alias_refresh_is_allowed(self):
        batch = self.batch()
        self.profile.observe({'wxid_a': '新名片'})
        self.assertTrue(self.profile.apply(dict(summary='合成概况', sources=['m1']), batch))
        self.assertFalse(self.profile.apply(dict(summary='过期结果', sources=['m1']), batch))
        other = self.batch(2)
        other['identity']['chat'] = 'other@chatroom'
        self.assertFalse(self.profile.apply(dict(summary='串群', sources=['m2']), other))
        self.assertEqual(self.profile.analyzed_through, 1)
        self.assertEqual(self.profile.data['members']['wxid_a']['alias'], '新名片')

    def test_unchanged_summary_can_advance_past_nontext_without_model_evidence(self):
        batch = self.profile.analysis_snapshot([], scan_through=7)
        self.assertTrue(self.profile.apply(dict(summary='', sources=[]), batch))
        self.assertEqual(GroupProfile(self.path, IDENTITY).analyzed_through, 7)
        batch['through'] = 6
        batch['token'] = self.profile._token()
        with self.assertRaisesRegex(ValueError, 'profile_invalid_watermark'):
            self.profile.apply(dict(summary='', sources=[]), batch)

    def test_external_edit_or_delete_is_not_overwritten(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.path.write_bytes(self.path.read_bytes() + b' ')
                if remove:
                    self.path.unlink()
                with self.assertRaisesRegex(ValueError, 'profile_changed_on_disk'):
                    self.profile.observe({'wxid_a': '不能覆盖'})
                if not self.path.exists():
                    break

    def test_failed_atomic_replace_keeps_previous_file_and_cleans_temporary(self):
        before = self.path.read_bytes()
        with patch('wxbridge.storage.group_profile.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.profile.observe({'wxid_a': '改名'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob('.profile-*')))
        self.assertEqual(self.profile.data['members']['wxid_a']['alias'], '同名')

    def test_corrupt_duplicate_keys_and_unexpected_member_fields_fail_closed(self):
        for body in ('{', '{"summary":"one","summary":"two"}',
                     json.dumps(dict(self.profile.data, members={'wxid_a': dict(alias='名', name='多余')}))):
            with self.subTest(body=body):
                self.path.write_text(body, encoding='utf-8')
                with self.assertRaises(ValueError):
                    GroupProfile(self.path, IDENTITY)


class ProfileAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.payload = dict(chat='合成群', bot='wxid_bot', summary='',
            members={'wxid_a': dict(alias='同名', memory='不应传给概况模型'),
                     'wxid_b': dict(alias='同名')},
            messages=[dict(id='m1', sender='wxid_a', time=NOW, text='合成证据', is_bot=False,
                           reference=dict(sender='wxid_b', text='引用证据', raw_xml='不应传出'))])

    def test_prompt_preserves_identity_and_filters_private_or_transport_fields(self):
        saved = deepcopy(self.payload)
        prompt = analysis.build_prompt(self.cfg, self.payload)
        public = json.loads(prompt[1]['content'])
        self.assertEqual(public['messages'][0]['sender'], 'wxid_a')
        self.assertEqual(public['messages'][0]['reference'], dict(sender='wxid_b', text='引用证据'))
        self.assertEqual(public['members']['wxid_a'], dict(alias='同名'))
        self.assertEqual(self.payload, saved)
        self.assertNotIn('不应传', prompt[1]['content'])

    def test_result_strictly_binds_typed_sources_and_forbids_member_updates(self):
        invalid = [dict(summary='新概况', sources=[]), dict(summary='新概况', sources=[1]),
                   dict(summary='新概况', sources=[True]), dict(summary='新概况', sources=['m2']),
                   dict(summary='新概况', sources=['m1', 'm1']),
                   dict(summary='新概况', sources=['m1'], members={})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(analysis.ProfileAnalysisError):
                analysis.parse_result(self.cfg, self.payload, json.dumps(value))
        self.assertEqual(analysis.parse_result(self.cfg, self.payload,
            '{"summary":"合成概况","sources":["m1"]}'), dict(summary='合成概况', sources=['m1']))
        with self.assertRaisesRegex(analysis.ProfileAnalysisError, 'profile_duplicate_result_key'):
            analysis.parse_result(self.cfg, self.payload, '{"summary":"","summary":"","sources":[]}')

    def test_empty_batch_does_not_create_or_call_client(self):
        self.payload['messages'] = []
        with patch('wxbridge.ai.client.client_for') as client:
            self.assertIsNone(analysis.analyze(self.cfg, self.payload))
            client.assert_not_called()

    def test_invalid_inputs_and_size_limits_are_rejected_before_client(self):
        for change in (dict(messages=[dict(self.payload['messages'][0], sender='unknown')]),
                       dict(messages=self.payload['messages']*2),
                       dict(messages=[dict(self.payload['messages'][0], time=True)]),
                       dict(messages=[dict(self.payload['messages'][0], text='X'*25000)])):
            with self.subTest(change=list(change)), self.assertRaises(analysis.ProfileAnalysisError):
                analysis.build_prompt(self.cfg, dict(self.payload, **change))

    def test_is_bot_cannot_relabel_another_sender_or_hide_the_actual_bot(self):
        for sender, is_bot in (('wxid_a', True), ('wxid_bot', False), ('unknown', True)):
            value = dict(self.payload, messages=[dict(self.payload['messages'][0], sender=sender, is_bot=is_bot)])
            with self.subTest(sender=sender, is_bot=is_bot), self.assertRaises(analysis.ProfileAnalysisError):
                analysis.build_prompt(self.cfg, value)


class ProfileControllerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        cfg = default_config()
        cfg['_instance_config_file'] = str(Path(temporary.name)/'config.json')
        cfg['profile'].update(enabled=True, batch_size=2, min_new_messages=2)
        store = Mock(self_wxid='wxid_bot')
        store.contacts.return_value = {'wxid_bot': dict(nick_name='机器人'),
            'wxid_a': dict(nick_name='昵称甲', remark='备注甲', alias='微信号甲'),
            'wxid_b': dict(nick_name='昵称乙', remark='备注乙')}
        store.group_member_aliases.return_value = {'wxid_a': '群名片', 'wxid_b': ''}
        store.recent_messages.return_value = []
        store.messages_after.return_value = []
        self.session = SimpleNamespace(cfg=cfg, store=store, chat_id=IDENTITY['chat'],
            chat_title='合成群', journal={'identity': IDENTITY}, memory=None, last_local_id=10,
            models=Mock(), metrics=Mock(), has_foreground_tasks=Mock(return_value=False), job_factory=Mock())
        self.session.models.status.return_value = {'background': dict(running=False, queued=0)}
        self.clock = patch('wxbridge.app.group_profile.time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.controller = ProfileController(self.session)
        self.addCleanup(self.controller.close)

    def test_group_card_then_nickname_never_remark_or_wechat_account_alias(self):
        members = self.controller.document.data['members']
        self.assertEqual(members['wxid_a']['alias'], '群名片')
        self.assertEqual(members['wxid_b']['alias'], '昵称乙')
        self.session.store.group_member_aliases.return_value = {'wxid_a': ''}
        self.controller.aliases_at = 0
        self.controller.observe([])
        self.assertEqual(self.controller.document.data['members']['wxid_a']['alias'], '昵称甲')

    def test_missing_group_snapshot_preserves_card_and_private_chat_uses_nickname(self):
        self.session.store.group_member_aliases.return_value = {}
        self.controller.aliases_at = 0
        self.controller.observe([])
        self.assertEqual(self.controller.document.data['members']['wxid_a']['alias'], '群名片')
        self.session.chat_id = 'wxid_a'
        self.controller.aliases_at = 0
        self.controller.observe([])
        self.assertEqual(self.controller.document.data['members']['wxid_a']['alias'], '昵称甲')

    def test_new_participant_during_refresh_interval_gets_nickname_immediately(self):
        self.session.store.contacts.return_value['wxid_new'] = dict(nick_name='新成员', remark='备注')
        self.controller.observe([msg(1, NOW, '新成员发言', 'wxid_new')])
        self.assertEqual(self.controller.document.data['members']['wxid_new']['alias'], '新成员')

    def test_background_or_foreground_work_prevents_profile_submission(self):
        self.session.store.recent_messages.return_value = [msg(1, NOW, '文本', 'wxid_a')]
        for running, queued, foreground in ((True, 0, False), (False, 1, False), (False, 0, True)):
            self.session.models.status.return_value['background'].update(running=running, queued=queued)
            self.session.has_foreground_tasks.return_value = foreground
            self.controller.step()
        self.session.job_factory.assert_not_called()

    def test_completed_proposal_is_applied_only_when_owner_collects(self):
        rows = [msg(i, NOW, f'文本{i}', 'wxid_a') for i in (1, 2)]
        self.session.store.recent_messages.return_value = rows
        job = Mock()
        job.poll.return_value = None
        self.session.job_factory.return_value = job
        self.controller.step()
        self.session.job_factory.assert_called_once()
        self.assertEqual(self.controller.document.analyzed_through, 0)
        job.poll.return_value = dict(ok=True, proposal=dict(summary='新概况', sources=['m1']))
        self.assertEqual(self.controller.document.data['summary'], '')
        self.controller.step(collect_only=True)
        self.assertEqual(self.controller.document.analyzed_through, 2)
        self.assertEqual(self.controller.document.data['summary'], '新概况')
        job.close.assert_called_once()
        self.assertIsNone(self.controller.pending)

    def test_failed_and_stale_results_do_not_advance_watermark(self):
        self.session.store.recent_messages.return_value = [msg(1, NOW, '文本', 'wxid_a')]
        job = self.session.job_factory.return_value
        job.poll.return_value = None
        self.controller.step()
        job.poll.return_value = dict(ok=False, error='profile_invalid_result')
        self.controller.step(collect_only=True)
        self.assertEqual(self.controller.status()['error'], 'profile_invalid_result')
        self.assertEqual(self.controller.document.analyzed_through, 0)
        self.assertGreater(self.controller.next_at, NOW)

    def test_nontext_batch_advances_without_model_and_close_discards_pending(self):
        self.session.store.recent_messages.return_value = [dict(msg(1, NOW, '', 'wxid_a'), type=47)]
        self.controller.step()
        self.assertEqual(self.controller.document.analyzed_through, 1)
        self.session.job_factory.assert_not_called()
        self.controller.job = Mock()
        job = self.controller.job
        self.controller.pending = {'synthetic': True}
        self.controller.close()
        job.close.assert_called_once()
        self.assertIsNone(self.controller.pending)

    def test_incremental_batches_are_throttled_and_never_read_past_received_cursor(self):
        self.controller.document.apply(dict(summary='', sources=[]),
            self.controller.document.analysis_snapshot([], scan_through=5))
        self.session.store.messages_after.return_value = [msg(6, NOW, '新文本', 'wxid_a'),
                                                         msg(11, NOW, '尚未接收', 'wxid_a')]
        self.controller.step()
        self.session.job_factory.assert_not_called()
        self.assertEqual(self.controller.document.analyzed_through, 5)

    def test_participants_include_nested_quotes_mentions_and_pat_without_group_ids(self):
        row = dict(msg(1, NOW, '文本', 'wxid_a'), mentioned_users=['wxid_b', 'notify@all'],
                   pat=dict(target='wxid_c'), reference=dict(sender='wxid_d',
                   reference=dict(sender='other@chatroom')))
        self.assertEqual(participant_ids([row], 'wxid_bot'),
                         {'wxid_bot', 'wxid_a', 'wxid_b', 'wxid_c', 'wxid_d'})
