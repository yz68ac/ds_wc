"""参与判断与正文生成的边界、剩余期限和抢占；无真实API调用。"""
from model_support import default_config
from contextlib import nullcontext
import copy
import json
import unittest
from unittest.mock import Mock, patch

from model_support import ClientFixture, ModelError
from wxbridge.ai.proactive import build_prompt, evaluate, parse_judgment, ProactiveError
from wxbridge.config import DEFAULTS
from synthetic import NOW, msg
import test_conversation as conversation


def response(text):
    return {'choices': [{'message': {'content': text, 'reasoning_content': 'PRIVATE_REASONING'}}]}


class StageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(Path(__file__).resolve().parents[1]/'prompts/system.md')
        self.prompt = build_prompt(self.cfg, [msg(7, NOW, '合成接话目标')], '合成群', opportunity=dict(source_ids=[7]))

    def test_strict_judgment_does_not_accept_generated_text_or_unusable_target(self):
        for value in (dict(speak=True, text='不许预生成', reply_to=7), dict(speak=1, reply_to=7),
                      dict(speak=True, reply_to=True), dict(speak=True, reply_to=0),
                      dict(speak=False, reply_to=7), dict(speak=True), []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_judgment(json.dumps(value))












    def test_worker_preserves_only_known_error_code_and_length(self):
        import logging
        from wxbridge.app.workers import _work
        for code, expected in (('proactive_invalid_reply_length', 'proactive_invalid_reply_length'),
                               ('PRIVATE_MODEL_OUTPUT', 'proactive_worker_failed')):
            connection = Mock()
            prior = logging.root.manager.disable
            try:
                with patch('wxbridge.ai.proactive.evaluate', side_effect=ProactiveError(code, reply_chars=41)):
                    _work(connection, 'proactive', ({}, []))
            finally:
                logging.disable(prior)
            self.assertEqual(connection.send.call_args.args[0], dict(ok=False, error=expected, reply_chars=41))


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

    def test_body_length_failure_reaches_status_without_body_and_without_send(self):
        self.start_reply().result = dict(ok=False, error='proactive_invalid_reply_length', reply_chars=41,
                                        body='PRIVATE_MODEL_OUTPUT')
        self.bot.tick()
        state = self.bot.proactive.state()
        self.assertEqual(state['error'], 'proactive_invalid_reply_length')
        self.assertEqual(state['reply_chars'], 41)
        self.assertNotIn('PRIVATE_MODEL_OUTPUT', json.dumps(state))
        self.assertFalse(self.sends())

    def test_unknown_worker_error_and_invalid_metadata_cannot_leak_to_status(self):
        self.start_reply().result = dict(ok=False, error='PRIVATE_MODEL_OUTPUT', reply_chars='PRIVATE_MODEL_OUTPUT')
        self.bot.tick()
        state = self.bot.proactive.state()
        self.assertEqual(state['error'], 'proactive_worker_failed')
        self.assertIsNone(state['reply_chars'])
        self.assertNotIn('PRIVATE_MODEL_OUTPUT', json.dumps(state))
