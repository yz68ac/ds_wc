"""句中点名按上下文唤起，绕过随机入场限制但保留队列确认和真实@优先。"""
import unittest
import copy
from unittest.mock import patch

from wxbridge.ai.client import DeepSeekClient
from wxbridge.ai.proactive import addresses_bot, evaluate
import test_conversation as conversation


class KeywordParticipationTests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = conversation.ConversationTests.add
    sends = conversation.ConversationTests.sends
    restart = conversation.ConversationTests.restart
    ordinary = conversation.ConversationTests.ordinary
    decision = conversation.ConversationTests.decision
    confirm = conversation.ConversationTests.confirm

    def test_name_in_middle_and_end_case_insensitive_and_chinese_neighbors(self):
        for text in ('ds', 'DS你怎么看', '这事让ds来评评', '刚才那句说的是ds', '我觉得DS说得有道理', '@ds 这怎么看'):
            with self.subTest(text=text):
                self.assertTrue(addresses_bot(dict(text=text), self.cfg, 'wxid_self'))
        for text in ('ads', 'dss', 'cads_test', 'ds123', '123ds', 'ds_bot'):
            with self.subTest(text=text):
                self.assertFalse(addresses_bot(dict(text=text), self.cfg, 'wxid_self'))

    def test_single_keyword_message_bypasses_entry_limits_but_waits_for_send_cooldown(self):
        self.cfg['proactive']['max_entries_per_hour'] = 3
        self.cfg['target']['cooldown_seconds'] = 8
        with self.bot.journal.db:
            self.bot.journal.set('proactive_entries', [self.now-1, self.now-100, self.now-200])
            self.bot.journal.set('last_success', self.now)
        self.bot.proactive.update(last_evaluated_at=self.now, evaluations=1)
        row = self.ordinary('这事让ds来评评')
        self.assertEqual(self.bot.proactive.pending['reason'], 'addressed')
        self.assertEqual(self.bot.proactive.pending['source_ids'], [row['local_id']])
        self.decision(text='这局你确实有点冤')
        self.assertFalse(self.sends())
        self.now += 8
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.confirm()
        self.assertTrue(self.bot.journal.status()['conversation']['active'])

    def test_new_keyword_after_silent_decision_uses_ten_second_check_not_sixty(self):
        self.ordinary('ds最近不说话了')
        self.decision(speak=False)
        count = len(self.jobs)
        row = self.ordinary('那让DS来评评刚才的话')
        self.assertEqual(len(self.jobs), count)
        self.now += 10
        self.bot.tick()
        self.assertEqual(len(self.jobs), count+1)
        self.assertEqual(self.bot.proactive.pending['source_ids'], [row['local_id']])

    def test_disabled_opportunities_and_other_recipient_do_not_wake_from_keyword(self):
        self.cfg['proactive']['opportunities']['enabled'] = False
        self.ordinary('这事让ds来评评')
        self.assertFalse(self.jobs)
        self.cfg['proactive']['opportunities']['enabled'] = True
        self.now += 61
        self.ordinary('你觉得ds怎样', mentioned_users=['wxid_other'])
        self.assertFalse(self.jobs)

    def test_unrelated_conversation_still_obeys_entry_cooldown(self):
        with self.bot.journal.db:
            self.bot.journal.set('proactive_entries', [self.now-1])
        self.ordinary('今天又输了几把')
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.bot.proactive.state()['outcome'], 'entry_cooldown')

    def test_old_keyword_in_history_or_reference_body_is_not_a_new_invitation(self):
        self.ordinary('ds之前说过这个')
        self.decision(speak=False)
        self.now += 10
        self.ordinary('我同意这句话', reference=dict(sender='wxid_other', kind='text', text='让ds来评评'))
        self.assertIsNone(self.bot.proactive.job)
        self.now += 60
        self.bot.tick()
        self.assertIsNone(self.bot.proactive.job)

    def test_new_body_can_call_ds_about_another_members_quote_during_window(self):
        self.add(1)
        self.bot.tick()
        next(j for j in self.jobs if j.kind == 'generate').result = dict(ok=True, reply='正常回复')
        self.bot.tick()
        self.confirm()
        self.now += 10
        row = self.ordinary('这句话ds怎么看', sender='wxid_other',
            reference=dict(sender='wxid_friend', kind='text', text='被讨论的内容', available=True))
        self.assertEqual(self.bot.proactive.pending['reason'], 'addressed')
        self.assertEqual(self.bot.proactive.pending['source_ids'], [row['local_id']])
        prompt = self.bot.proactive.job[0].payload[1]
        self.assertIn('被讨论的内容', prompt[1]['content'])
        self.assertIn('优先结合上下文自然回应', prompt[0]['content'])

    def test_keyword_judges_without_thinking_then_replies_max_and_can_be_preempted(self):
        self.ordinary('这事ds怎么看')
        job = self.bot.proactive.job[0]
        cfg, prompt, options = job.payload
        self.assertEqual(options['source_ids'], [1])
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                patch.object(DeepSeekClient, '_post', side_effect=[
                    {'choices': [{'message': {'content': '{"speak":true,"reply_to":1}'}}]},
                    {'choices': [{'message': {'content': '我觉得你漏看了前提'}}]}]) as post:
            decision=evaluate(cfg, prompt, **options)
            self.assertTrue(decision['handoff'])
            self.assertEqual(post.call_count,1)
        judge = post.call_args_list[0].args[0]
        self.assertEqual(judge['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', judge)
        self.assertEqual(judge['max_tokens'], 256)
        from wxbridge.ai.roles import model_settings
        self.assertEqual(model_settings(cfg,'reply')['thinking'],'enabled')
        self.assertEqual(model_settings(cfg,'reply')['reasoning_effort'],'max')
        self.assertEqual(cfg['deepseek']['reasoning_effort'], 'max')
        self.add(2)
        self.bot.tick()
        self.assertTrue(job.closed)
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.bot.journal.head()['trigger_kind'], 'mention')

    def test_keyword_send_after_restart_confirms_once_without_replaying_word(self):
        self.ordinary('刚才那个说法ds怎么看')
        self.decision(text='我觉得你漏看了前提')
        self.restart()
        self.confirm()
        self.assertEqual(len(self.sends()), 1)
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertTrue(self.bot.journal.status()['conversation']['active'])

    def test_unnamed_entry_uses_non_thinking_participation_worker(self):
        from unittest.mock import Mock
        from wxbridge.app.workers import _work
        import logging
        self.ordinary('今天又输了几把')
        self.ordinary('我也差不多', sender='wxid_other')
        self.now += 10
        self.ordinary('看来得换个打法')
        job = self.bot.proactive.job[0]
        self.assertEqual(job.payload[2]['source_ids'], [1, 2, 3])
        before = copy.deepcopy(self.cfg)
        connection = Mock()
        disabled = logging.root.manager.disable
        try:
            with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                    patch.object(DeepSeekClient, '_post', return_value={'choices': [{'message': {
                        'content': '{"speak":false,"reply_to":null}'}}]}) as post:
                _work(connection, 'proactive', job.payload)
        finally:
            logging.disable(disabled)
        self.assertTrue(connection.send.call_args.args[0]['ok'])
        self.assertEqual(connection.send.call_args.args[0]['model_metrics'][0]['role'], 'participation')
        payload = post.call_args.args[0]
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', payload)
        self.assertEqual(payload['max_tokens'], 256)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.cfg, before)

    def test_window_followup_and_new_keyword_both_use_two_stage_worker(self):
        self.ordinary('这事让ds来评评')
        self.decision()
        self.confirm()
        self.now += 10
        self.ordinary('你再说一句')
        self.assertEqual(self.bot.proactive.job[0].payload[2]['source_ids'], [3])
        self.decision(speak=False)
        self.now += 10
        self.ordinary('那DS怎么看后面那句')
        self.assertEqual(self.bot.proactive.job[0].payload[2]['source_ids'], [4])

    def test_quote_without_name_uses_same_worker_options(self):
        self.ordinary('这个说得不错', reference=dict(sender='wxid_self', kind='text', text='旧回复'))
        self.assertEqual(self.bot.proactive.pending['reason'], 'addressed')
        self.assertEqual(self.bot.proactive.job[0].payload[2]['source_ids'], [1])

    def test_keyword_worker_inherits_reply_overrides_and_request_deadline(self):
        from unittest.mock import Mock
        from wxbridge.app.workers import _work
        import logging
        self.cfg['models']['reply'] = dict(model='synthetic-reply', thinking='enabled',
                                          reasoning_effort='max', timeout_seconds=5, max_tokens=1700)
        self.ordinary('这事ds怎么看')
        job = self.bot.proactive.job[0]
        before = copy.deepcopy(self.cfg)
        connection = Mock()
        disabled = logging.root.manager.disable
        try:
            with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                    patch.object(DeepSeekClient, '_post', side_effect=[
                        {'choices': [{'message': {'content': '{"speak":true,"reply_to":1}'}}],
                         'usage': dict(prompt_tokens=10, completion_tokens=5)},
                        {'choices': [{'message': {'content': '这局你确实有点冤'}}],
                         'usage': dict(prompt_tokens=20, completion_tokens=8)}]) as post:
                _work(connection, 'proactive', job.payload)
        finally:
            logging.disable(disabled)
        result = connection.send.call_args.args[0]
        self.assertTrue(result['ok'])
        self.assertEqual(result['model_metrics'][0]['role'], 'participation')
        self.assertEqual(result['decision']['reply_to'], 1)
        self.assertEqual(len(result['model_metrics']), 1)
        self.assertEqual(connection.send.call_count, 1)
        self.assertTrue(result['decision']['handoff'])
        self.assertNotIn('synthetic-key', str(connection.send.call_args_list))
        payload = post.call_args.args[0]
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        job.result=result
        self.bot.tick();self.bot.tick()
        generated=next(j for j in self.jobs if j.kind=='generate')
        self.assertEqual(generated.payload[0]['models']['reply']['model'],'synthetic-reply')
        self.assertEqual(self.bot.journal.head()['trigger_kind'],'addressed')
        self.assertEqual(self.cfg, before)
