"""参与判断与正文生成的边界、剩余期限和抢占；无真实API调用。"""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.client import DeepSeekClient, DeepSeekError
from wxbridge.ai.proactive import build_prompt, evaluate, parse_judgment
from wxbridge.config import DEFAULTS
import test_conversation as conversation


def response(text):
    return {'choices': [{'message': {'content': text, 'reasoning_content': 'PRIVATE_REASONING'}}]}


class StageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['deepseek'].update(system_prompt_file='', system_prompt='合成人格')
        self.prompt = build_prompt(self.cfg, [], '合成群', opportunity=dict(source_ids=[7]))

    def test_strict_judgment_does_not_accept_generated_text_or_unusable_target(self):
        for value in (dict(speak=True, text='不许预生成', reply_to=7), dict(speak=1, reply_to=7),
                      dict(speak=True, reply_to=True), dict(speak=True, reply_to=0),
                      dict(speak=False, reply_to=7), dict(speak=True), []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_judgment(json.dumps(value))

    def test_invalid_or_unknown_target_never_calls_reply_model(self):
        for text in ('invalid', '{"speak":true,"reply_to":999}', '{"speak":true,"reply_to":7,"text":"预生成"}'):
            with self.subTest(text=text), patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                    patch.object(DeepSeekClient, '_post', return_value=response(text)) as post, self.assertRaises(ValueError):
                try:
                    evaluate(self.cfg, self.prompt, source_ids=[7])
                finally:
                    self.assertEqual(post.call_count, 1)

    def test_unnamed_entry_uses_max_for_body_and_keeps_context_and_persona(self):
        callback = Mock()
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                patch.object(DeepSeekClient, '_post', side_effect=[
                    response('{"speak":true,"reply_to":7}'), response('**这局有点意思。**')]) as post:
            answer = evaluate(self.cfg, self.prompt, source_ids=[7], on_reply=callback)
        callback.assert_called_once_with(7)
        self.assertEqual(answer, dict(speak=True, text='这局有点意思', reply_to=7))
        judge, reply = [call.args[0] for call in post.call_args_list]
        self.assertEqual(judge['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', judge)
        self.assertEqual(reply['thinking'], {'type': 'enabled'})
        self.assertEqual(reply['reasoning_effort'], 'max')
        self.assertEqual(judge['messages'][1], reply['messages'][1])
        self.assertNotIn('合成人格', judge['messages'][0]['content'])
        self.assertTrue(reply['messages'][0]['content'].startswith('合成人格'))
        self.assertIn('消息ID 7', reply['messages'][0]['content'])
        self.assertNotIn('PRIVATE_REASONING', json.dumps(answer))

    def test_silence_is_one_request_and_never_emits_reply_event(self):
        callback = Mock()
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                patch.object(DeepSeekClient, '_post', return_value=response('{"speak":false,"reply_to":null}')) as post:
            self.assertFalse(evaluate(self.cfg, self.prompt, source_ids=[7], on_reply=callback)['speak'])
        self.assertEqual(post.call_count, 1)
        callback.assert_not_called()

    def test_elapsed_judgment_leaves_only_remaining_lifetime_for_body(self):
        clock = [100.0]
        timeouts = []
        def post(payload, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                clock[0] += 12
                return response('{"speak":true,"reply_to":7}')
            return response('确实有点东西')
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                patch('wxbridge.ai.proactive.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(DeepSeekClient, '_post', side_effect=post):
            evaluate(self.cfg, self.prompt, source_ids=[7], timeout_seconds=20)
        self.assertEqual(timeouts, [20, 8])

    def test_no_api_after_deadline_or_second_call_after_expired_judgment(self):
        clock = [100.0]
        callback = Mock()
        def expired(payload, timeout):
            clock[0] += 11
            return response('{"speak":true,"reply_to":7}')
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'), \
                patch('wxbridge.ai.proactive.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(DeepSeekClient, '_post', side_effect=expired) as post:
            with self.assertRaises(DeepSeekError):
                evaluate(self.cfg, self.prompt, source_ids=[7], timeout_seconds=0)
            post.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'expired_before_reply'):
                evaluate(self.cfg, self.prompt, source_ids=[7], timeout_seconds=10, on_reply=callback)
            self.assertEqual(post.call_count, 1)
        callback.assert_not_called()


class StagePipelineTests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = conversation.ConversationTests.add
    sends = conversation.ConversationTests.sends
    restart = conversation.ConversationTests.restart
    ordinary = conversation.ConversationTests.ordinary
    decision = conversation.ConversationTests.decision
    confirm = conversation.ConversationTests.confirm

    def start_reply(self):
        self.ordinary('ds这局怎么看')
        job = self.bot.proactive.job[0]
        job.result = dict(event='proactive_reply_started', reply_to=1,
                          model_metrics=[dict(role='participation', elapsed_ms=10, usage={})])
        with patch.object(self.bot.metrics, 'record_models', wraps=self.bot.metrics.record_models) as record:
            self.bot.tick()
        self.assertEqual(record.call_count, 1)
        job.result = None  # 模拟Pipe事件已消费。
        self.assertFalse(job.closed)
        self.assertEqual(self.bot.proactive.state()['outcome'], 'generating_reply')
        self.assertEqual(self.bot.proactive.state()['model_role'], 'reply')
        self.assertFalse(self.sends())
        return job

    def test_phase_event_keeps_worker_and_final_response_uses_send_confirmation(self):
        job = self.start_reply()
        self.now += 1
        self.decision(text='这局你可真敢打')
        self.assertTrue(job.closed)
        self.assertEqual(len(self.sends()), 1)
        self.confirm()
        self.assertTrue(self.bot.journal.status()['conversation']['active'])

    def test_real_mention_can_preempt_body_generation(self):
        job = self.start_reply()
        self.add(2)
        self.bot.tick()
        self.assertTrue(job.closed)
        self.assertIsNone(self.bot.proactive.job)
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.journal.head()['trigger_kind'], 'mention')

    def test_body_failure_or_late_result_never_sends_placeholder(self):
        self.start_reply().result = dict(ok=False, error='connection_failed')
        self.bot.tick()
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.proactive.state()['outcome'], 'error')
        self.now += 10
        self.ordinary('那ds怎么看后面的')
        self.now += self.cfg['proactive']['reply_ttl_seconds']+1
        self.decision()
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.proactive.state()['outcome'], 'expired')
