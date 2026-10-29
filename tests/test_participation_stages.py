"""Decision/generation boundary, deadlines and safe worker failures; offline only."""
import json
import unittest
from unittest.mock import patch

from model_support import default_config
from wxbridge.ai.participation import ParticipationError, parse_decision
from wxbridge.app.model_runtime import GroupModels
import test_conversation as conversation


class StageTests(unittest.TestCase):
    def test_decision_rejects_body_generation_and_old_single_reply_protocol(self):
        for value in (dict(reply_to=[7], text='不许预生成'), dict(speak=True, reply_to=7),
                      dict(reply_to=[True]), dict(reply_to=[0]), dict(reply_to=[8]), []):
            with self.subTest(value=value), self.assertRaises(ParticipationError):
                parse_decision(json.dumps(value), [7])

    def test_model_lane_preserves_fixed_error_codes_and_hides_arbitrary_model_output(self):
        cfg = default_config()
        for code, expected in (('participation_invalid_reply_target', 'participation_invalid_reply_target'),
                               ('PRIVATE_MODEL_OUTPUT', 'participation_worker_failed')):
            runtime = GroupModels(cfg)
            try:
                with patch('wxbridge.ai.participation.evaluate', side_effect=ParticipationError(code)):
                    result = runtime.submit('participation', (cfg, [], dict(source_ids=[7])), timeout=5).future.result(timeout=2)
            finally:
                runtime.close()
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], expected)
            self.assertNotIn('reply_chars', result)
            self.assertNotIn('PRIVATE_MODEL_OUTPUT', json.dumps(result))


class StagePipelineTests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    make_bot = conversation.ConversationTests.make_bot
    add = conversation.ConversationTests.add
    sends = conversation.ConversationTests.sends
    restart = conversation.ConversationTests.restart
    ordinary = conversation.ConversationTests.ordinary
    confirm = conversation.ConversationTests.confirm

    def start_reply(self):
        row = self.ordinary('这局怎么看')
        job = self.bot.participation.job
        self.assertFalse(any(worker.kind == 'generate' for worker in self.jobs))
        job.result = dict(ok=True, decision=dict(reply_to=[row['local_id']]),
                          model_metrics=[dict(role='participation', elapsed_ms=10, usage={})])
        with patch.object(self.bot.metrics, 'record_models', wraps=self.bot.metrics.record_models) as record:
            self.bot.tick()
        self.assertTrue(any(call.args[0] == job.result['model_metrics'] for call in record.call_args_list))
        self.assertTrue(job.closed)
        self.assertIsNone(self.bot.participation.job)
        self.assertFalse(self.sends())
        return next(worker for worker in self.jobs if worker.kind == 'generate' and not worker.closed)

    def test_approved_message_starts_separate_full_reply_and_uses_send_confirmation(self):
        job = self.start_reply()
        body = '这是需要完整解释的回复。'*8
        self.now += 1
        job.result = dict(ok=True, reply=body)
        self.bot.tick()
        self.assertTrue(job.closed)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0].payload[2], body)
        self.confirm()
        self.assertFalse(self.bot.journal.tasks())

    def test_new_real_mention_does_not_preempt_valid_body_generation(self):
        first = self.start_reply()
        self.add(2)
        self.bot.tick()
        self.assertFalse(first.closed)
        self.assertIsNotNone(self.bot.participation.job)
        self.assertEqual(self.bot.participation.pending['source_ids'], [2])
        first.result = dict(ok=True, reply='第一条仍然有效')
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0].payload[2], '第一条仍然有效')

    def test_expired_judgment_cannot_start_generation_or_send(self):
        row = self.ordinary('想听听你的看法')
        job = self.bot.participation.job
        self.now += self.cfg['target']['max_message_age_seconds']+1
        job.result = dict(ok=True, decision=dict(reply_to=[row['local_id']]))
        self.bot.tick()
        self.assertFalse(self.sends())
        self.assertFalse(any(worker.kind == 'generate' for worker in self.jobs))
        self.assertEqual(self.bot.journal.participation_status()['counts'].get('expired'), 1)

    def test_generation_transient_failure_uses_existing_retry_queue(self):
        worker = self.start_reply()
        worker.result = dict(ok=False, error='connection_failed', retryable=True)
        self.bot.tick()
        self.assertFalse(self.sends())
        task = self.bot.journal.head()
        self.assertEqual(task['trigger_kind'], 'participation')
        self.assertEqual(task['generation'], 'pending')
        self.assertGreater(task['generation_retry_at'], self.now)

    def test_failed_decision_keeps_fixed_error_metadata_without_any_reply(self):
        self.ordinary('需要判断')
        self.bot.participation.job.result = dict(ok=False, error='PRIVATE_MODEL_OUTPUT',
            reply_chars='PRIVATE_MODEL_OUTPUT', body='PRIVATE_MODEL_OUTPUT')
        self.bot.tick()
        state = self.bot.journal.participation_status()
        self.assertEqual(state['error'], 'participation_worker_failed')
        self.assertNotIn('PRIVATE_MODEL_OUTPUT', json.dumps(state))
        self.assertFalse(self.sends())
        self.assertFalse(any(worker.kind == 'generate' for worker in self.jobs))

    def test_invalid_decision_target_never_enters_reply_queue(self):
        self.ordinary('候选问题')
        self.bot.participation.job.result = dict(ok=True, decision=dict(reply_to=[999]))
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.journal.participation_status()['error'], 'participation_invalid_reply_target')


if __name__ == '__main__':
    unittest.main()
