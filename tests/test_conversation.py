"""主动入场与两轮接话的端到端合成回归，无微信或API请求。"""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from wxbridge.ai.proactive import addresses_bot, parse_decision
from wxbridge.config import DEFAULTS, load_config
from synthetic import NOW
import test_context_pipeline as pipeline


class ConversationTests(unittest.TestCase):
    setUp = pipeline.PipelineTests.setUp
    add = pipeline.PipelineTests.add
    sends = pipeline.PipelineTests.sends
    restart = pipeline.PipelineTests.restart

    def make_bot(self):
        self.cfg['proactive']['enabled'] = True
        self.cfg['queue']['debounce_seconds'] = 0
        pipeline.PipelineTests.make_bot(self)

    def ordinary(self, text, sender='wxid_friend', **fields):
        identifier = max([m['local_id'] for m in self.rows], default=0)+1
        row = self.add(identifier, sender=sender, text=text, mention=False)
        row.update(fields)
        self.bot.tick()
        return row

    def decision(self, text='你这是给对面上分呢', target=None, speak=True):
        if target is None and speak:
            target = self.bot.proactive.pending['source_ids'][-1]
        self.bot.proactive.job[0].result = dict(ok=True, decision=dict(speak=speak,
            text=text if speak else '', reply_to=target))
        self.bot.tick()

    def confirm(self):
        worker = self.sends()[-1]
        worker.result = dict(ok=True, ui=dict(submitted=True))
        return self.ordinary(worker.payload[2], sender='wxid_self')

    def enter(self):
        row = self.ordinary('ds，过来接个话')
        self.assertIsNotNone(self.bot.proactive.job)
        self.decision(target=row['local_id'])
        self.confirm()

    def test_name_is_only_a_candidate_and_confirmed_send_opens_session(self):
        row = self.ordinary('ds你怎么看')
        self.assertEqual(self.bot.proactive.pending['reason'], 'addressed')
        self.assertFalse(self.sends())
        self.decision(target=row['local_id'])
        self.assertFalse(self.bot.journal.get('proactive_session'))
        self.assertFalse(self.bot.journal.get('proactive_entries'))
        ack = self.confirm()
        session = self.bot.journal.get('proactive_session')
        self.assertEqual(session['anchor_id'], ack['local_id'])
        self.assertEqual(session['participant'], 'wxid_friend')
        self.assertEqual(session['followups'], 0)
        self.assertEqual(self.bot.journal.get('proactive_entries'), [self.now])

    def test_three_messages_from_two_members_can_evaluate_below_ten(self):
        self.ordinary('今天又输了五把')
        self.ordinary('我也一样', sender='wxid_other')
        self.now += 10
        self.ordinary('看来得换个打法')
        self.assertEqual(self.bot.proactive.pending['reason'], 'small_conversation')
        self.assertFalse(self.sends())
        self.decision(speak=False)
        self.assertFalse(self.sends())

    def test_single_member_unrelated_small_chat_does_not_trigger(self):
        for _ in range(3):
            self.ordinary('自己说自己的话')
        self.now += 10
        self.bot.tick()
        self.assertFalse(self.jobs)

    def test_name_boundaries_quote_and_explicit_other_mention(self):
        cfg = self.cfg
        for text in ('ads', 'dss', 'cads_test'):
            self.assertFalse(addresses_bot(dict(text=text), cfg, 'wxid_self'))
        self.assertTrue(addresses_bot(dict(text='ds你来看看'), cfg, 'wxid_self'))
        self.assertTrue(addresses_bot(dict(reference=dict(sender='wxid_self')), cfg, 'wxid_self'))
        self.assertFalse(addresses_bot(dict(text='ds你来看看', mentioned_users=['wxid_other']), cfg, 'wxid_self'))
        self.ordinary('ds说得对吧', mentioned_users=['wxid_other'])
        self.assertFalse(self.jobs)

    def test_two_followups_bypass_five_minute_silence_then_stop(self):
        self.enter()
        for count in range(1, 3):
            self.now += 10
            row = self.ordinary('你说得还挺有道理')
            self.assertEqual(self.bot.proactive.pending['mode'], 'followup')
            self.decision(text='那当然，听我的', target=row['local_id'])
            self.confirm()
            if count == 1:
                self.assertEqual(self.bot.journal.get('proactive_session')['followups'], 1)
        self.assertFalse(self.bot.journal.get('proactive_session'))
        self.now += 10
        self.ordinary('继续呢')
        self.assertEqual(len(self.sends()), 3)
        self.assertIsNone(self.bot.proactive.job)

    def test_member_reply_is_a_model_decision_not_automatic_send(self):
        self.enter()
        self.now += 10
        self.ordinary('嗯')
        self.decision(speak=False)
        self.assertEqual(len(self.sends()), 1)
        self.assertFalse(self.bot.journal.get('proactive_session'))

    def test_unrelated_member_does_not_become_a_reply_but_name_can_join(self):
        self.enter()
        self.now += 10
        self.ordinary('晚饭吃什么', sender='wxid_other')
        self.assertIsNone(self.bot.proactive.job)
        self.now += 10
        row = self.ordinary('ds这句说得还行', sender='wxid_other')
        self.assertEqual(self.bot.proactive.pending['source_ids'], [row['local_id']])
        self.decision(text='评价还挺勉强的嘛', target=row['local_id'])
        self.confirm()
        self.assertEqual(self.bot.journal.get('proactive_session')['participant'], 'wxid_other')

    def test_peer_addressing_someone_else_or_quoting_them_is_not_a_reply(self):
        self.enter()
        self.now += 10
        self.ordinary('你刚说的是什么', mentioned_users=['wxid_other'])
        self.assertIsNone(self.bot.proactive.job)
        self.now += 10
        self.ordinary('这个再说说', reference=dict(sender='wxid_other', kind='text', text='旧话', available=True))
        self.assertIsNone(self.bot.proactive.job)

    def test_prompt_contains_own_actual_message_and_only_fresh_source_ids(self):
        self.enter()
        self.now += 10
        row = self.ordinary('你刚刚在说谁')
        prompt = self.bot.proactive.job[0].payload[1]
        user = json.loads(prompt[1]['content'])
        self.assertTrue(any(m['member'] == 'BOT' and m['text'] == self.sends()[0].payload[2] for m in user['messages']))
        self.assertEqual(user['opportunity']['source_ids'], [row['local_id']])
        self.assertNotIn('participant', user['opportunity'])

    def test_invalid_reply_target_never_sends_or_consumes_session(self):
        self.enter()
        self.now += 10
        self.ordinary('是跟你说的')
        self.decision(target=1)  # 旧的入场消息不能作为新回应。
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.bot.proactive.state()['error'], 'ValueError')
        self.assertEqual(self.bot.journal.get('proactive_session')['followups'], 0)

    def test_manual_own_message_cancels_generated_followup(self):
        self.enter()
        self.now += 10
        self.ordinary('你再说一句')
        self.ordinary('手机人工发言', sender='wxid_self')
        self.decision(text='不应发出')
        self.assertEqual(len(self.sends()), 1)
        self.assertFalse(self.bot.journal.get('proactive_session'))

    def test_new_message_before_followup_submit_invalidates_old_generation(self):
        self.enter()
        self.now += 10
        self.ordinary('你刚说什么')
        self.ordinary('算了，换个话题')
        self.decision(text='过时的接话')
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.bot.proactive.state()['outcome'], 'context_changed')

    def test_silence_causes_no_calls_and_expiry_backs_off_entry(self):
        self.enter()
        count = len(self.jobs)
        self.now += 121
        self.bot.tick()
        self.assertFalse(self.bot.journal.get('proactive_session'))
        self.assertEqual(len(self.jobs), count)
        self.now += 200
        self.ordinary('ds在吗')
        self.assertEqual(len(self.jobs), count)

    def test_hourly_budget_blocks_entry_and_rolls_over(self):
        with self.bot.journal.db:
            self.bot.journal.set('proactive_entries', [self.now-100, self.now-200, self.now-300])
        self.ordinary('ds你怎么看')
        self.assertFalse(self.jobs)
        self.assertEqual(self.bot.proactive.state()['outcome'], 'entry_budget')
        self.now += 3601
        self.ordinary('ds现在呢')
        self.assertIsNotNone(self.bot.proactive.job)

    def test_restart_keeps_session_budget_and_does_not_repeat_old_reply(self):
        self.enter()
        self.now += 10
        self.ordinary('这个还挺好笑')
        self.decision(text='笑什么，你也跑不了')
        self.confirm()
        self.restart()
        self.now += 10
        self.bot.tick()
        self.assertIsNone(self.bot.proactive.job)
        self.assertEqual(self.bot.journal.get('proactive_session')['followups'], 1)
        self.now += 10
        self.ordinary('那你接着说')
        self.assertIsNotNone(self.bot.proactive.job)

    def test_restart_after_submit_only_confirms_and_creates_one_session(self):
        self.ordinary('ds来一句')
        self.decision()
        self.restart()
        self.confirm()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.bot.journal.get('proactive_entries')), 1)
        self.bot.tick()
        self.assertEqual(len(self.bot.journal.get('proactive_entries')), 1)

    def test_real_mention_preempts_followup_and_final_ack_opens_fresh_session(self):
        self.enter()
        self.now += 10
        self.ordinary('接着说呀')
        old = self.bot.proactive.job[0]
        row = self.add(4, sender='wxid_other')
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertFalse(self.bot.journal.get('proactive_session'))
        self.jobs[-1].result = dict(ok=True, reply='正常回答真实提问')
        self.bot.tick()
        self.confirm()
        self.assertEqual(self.bot.journal.get('proactive_session')['participant'], row['sender'])
        self.assertEqual(len(self.bot.journal.get('proactive_entries')), 1)

    def test_database_failure_before_send_discards_optional_reply_and_keeps_intake(self):
        self.ordinary('ds来一句')
        self.cfg['target']['cooldown_seconds'] = 8
        with self.bot.journal.db:
            self.bot.journal.set('last_success', self.now)
        self.decision()
        self.store.messages_in_window.side_effect = ValueError('PRIVATE_ERROR')
        self.now += 9
        self.bot.tick()
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.proactive.state()['error'], 'ValueError')
        self.ordinary('继续同步新消息')
        self.assertEqual(self.bot.last_local_id, 2)

    def test_status_and_logs_do_not_expose_session_member_or_body(self):
        with self.assertLogs('wxbridge', level='INFO') as captured:
            self.ordinary('ds PRIVATE_TEXT', sender='wxid_PRIVATE_PERSON')
            self.decision()
            self.confirm()
        output = json.dumps(self.bot.journal.status())+' '.join(captured.output)
        self.assertNotIn('PRIVATE_TEXT', output)
        self.assertNotIn('wxid_PRIVATE_PERSON', output)
        self.assertTrue(self.bot.journal.status()['conversation']['active'])


class ConversationProtocolTests(unittest.TestCase):
    def test_target_protocol_rejects_invalid_or_silent_target(self):
        self.assertEqual(parse_decision('{"speak":true,"text":"接个话","reply_to":7}')['reply_to'], 7)
        for target in (True, '7', 0, -1, None):
            with self.subTest(target=target), self.assertRaises(ValueError):
                parse_decision(json.dumps(dict(speak=True, text='接话', reply_to=target)))
        with self.assertRaises(ValueError):
            parse_decision('{"speak":false,"text":"","reply_to":7}')

    def test_new_configuration_limits_and_old_override_merge(self):
        for field, value in [('max_entries_per_hour', True), ('entry_cooldown_seconds', 0),
                              ('opportunities', {'enabled': 1}), ('conversation', {'enabled': 'true'}),
                              ('conversation', {'enabled': True, 'max_followups': 3}),
                              ('opportunities', {'enabled': True, 'min_senders': 1}),
                              ('opportunities', {'enabled': True, 'address_names': ['']})]:
            defaults = copy.deepcopy(DEFAULTS)
            if isinstance(value, dict):
                defaults['proactive'][field].update(value)
            else:
                defaults['proactive'][field] = value
            with self.subTest(field=field, value=value), patch('wxbridge.config.DEFAULTS', defaults), patch('wxbridge.config.load_dotenv'):
                with self.assertRaises(ValueError):
                    load_config('__missing_synthetic_config__.json')


if __name__ == '__main__':
    unittest.main()
