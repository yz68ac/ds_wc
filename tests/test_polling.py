"""消息接收节流与发送前补读：合成时钟、SQLite和工作进程，不访问真实微信。"""
from model_support import default_config
from contextlib import nullcontext
from contextlib import nullcontext
import copy
import unittest
from unittest.mock import patch

from wxbridge.config import DEFAULTS, load_config
import test_context_pipeline as pipeline


class PollingTests(unittest.TestCase):
    add = pipeline.PipelineTests.add
    sends = pipeline.PipelineTests.sends
    restart = pipeline.PipelineTests.restart

    def setUp(self):
        pipeline.PipelineTests.setUp(self)
        self.clock = 100.
        clock = patch('wxbridge.app.bot.time.monotonic', side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def make_bot(self):
        self.cfg['poll_interval_seconds'] = 2
        self.cfg['queue']['debounce_seconds'] = 0
        self.cfg['proactive']['enabled'] = False
        pipeline.PipelineTests.make_bot(self)

    def advance(self, seconds):
        self.clock += seconds
        self.now += seconds

    def test_idle_queries_follow_config_instead_of_every_scheduler_tick(self):
        for _ in range(17):
            self.bot.tick()
            self.advance(.5)
        self.assertEqual(self.store.messages_after.call_count, 5)
        self.assertEqual(self.bot.metrics.stages['intake']['count'], 5)
        self.assertNotIn('pre_send_intake', self.bot.metrics.stages)
        self.assertFalse(self.jobs)

    def test_receive_waits_until_due_but_ready_reply_and_confirmation_do_not(self):
        self.bot.tick()
        self.add(1)
        self.advance(1.5)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.advance(.5)
        self.bot.tick()
        generator = self.jobs[-1]
        self.assertEqual(generator.kind, 'generate')
        generator.result = dict(ok=True, reply='合成回答')
        self.advance(.1)
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.bot.metrics.stages['intake']['count'], 2)
        self.assertEqual(self.bot.metrics.stages['pre_send_intake']['count'], 1)
        self.add(2, sender='wxid_self', text='合成回答', mention=False)
        self.advance(.1)
        self.bot.tick()
        self.assertEqual(self.bot.journal.status()['queue_length'], 0)
        self.assertEqual(self.bot.metrics.stages['intake']['count'], 2)

    def test_slow_generation_keeps_receiving_without_extra_empty_reads(self):
        self.add(1)
        self.bot.tick()
        self.add(2)
        for _ in range(4):
            self.advance(.5)
            self.bot.tick()
        self.assertEqual(len(self.bot.generators), 2)
        self.assertEqual(self.store.messages_after.call_count, 2)
        self.assertFalse(self.sends())

    def test_cooldown_blocked_and_inflight_tasks_do_not_trigger_pre_send_reads(self):
        self.cfg['target']['cooldown_seconds'] = 8
        self.add(1)
        self.bot.tick()
        with self.bot.journal.db:
            self.bot.journal.set('last_success', self.now)
        self.jobs[-1].result = dict(ok=True, reply='合成回答')
        self.advance(.5)
        self.bot.tick()
        self.assertEqual(self.store.messages_after.call_count, 1)
        self.assertFalse(self.sends())
        self.advance(7.5)
        with self.bot.journal.db:
            task = self.bot.journal.head()
            task['blocked_reason'] = 'synthetic_blocked'
            self.bot.journal.put(task)
        self.bot.tick()
        self.assertNotIn('pre_send_intake', self.bot.metrics.stages)
        with self.bot.journal.db:
            task['blocked_reason'] = ''
            self.bot.journal.put(task)
        self.advance(.5)
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.advance(.5)
        self.bot.tick()
        self.assertEqual(self.bot.metrics.stages['pre_send_intake']['count'], 1)

    def test_pre_send_read_accepts_new_independent_mention_without_dropping_either(self):
        self.add(1)
        self.bot.tick()
        self.jobs[-1].result = dict(ok=True, reply='第一个回答')
        self.advance(.5)
        self.add(2)
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 2)
        self.assertEqual(len(self.bot.journal.tasks()), 2)
        self.assertEqual(self.sends()[0].payload[2], '第一个回答')
        self.advance(.5)
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(2)['generation'], 'running')

    def test_new_real_mention_preempts_proactive_reply_during_pre_send_read(self):
        self.cfg['proactive']['enabled'] = True
        self.add(1, text='合成闲聊', mention=False)
        self.bot.tick()
        task = self.bot.journal.enqueue_proactive('合成主动短句', 1, self.now, self.now+60)
        self.advance(.5)
        self.add(2)
        self.bot.tick()
        self.assertFalse(self.sends())
        self.assertEqual(self.bot.journal.task(task['id'])['phase'], 'done')
        self.assertEqual(self.bot.journal.head()['trigger_kind'], 'mention')
        self.assertEqual(self.bot.last_local_id, 2)

    def test_optional_merge_reads_window_tail_before_sealing_and_never_sends_old_version(self):
        self.cfg['poll_interval_seconds'] = 4
        self.cfg['queue']['debounce_seconds'] = 3
        self.add(1)
        self.bot.tick()
        old = self.jobs[-1]
        old.result = dict(ok=True, reply='STALE_REPLY')
        self.advance(3.5)
        self.add(2, sent=int(self.now-1))
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()['phase'], 'open')
        self.assertFalse(self.sends())
        self.advance(.5)
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()['version'], 2)
        self.assertTrue(old.closed)
        self.assertEqual(self.bot.journal.head()['generation'], 'running')
        self.assertFalse(self.sends())

    def test_poll_deadline_uses_monotonic_clock_and_restart_polls_immediately(self):
        self.bot.tick()
        self.now += 3600
        self.advance(1)
        self.bot.tick()
        self.assertEqual(self.store.messages_after.call_count, 1)
        self.now -= 7200
        self.advance(1)
        self.bot.tick()
        self.assertEqual(self.store.messages_after.call_count, 2)
        self.restart()
        self.bot.tick()
        self.assertEqual(self.store.messages_after.call_count, 3)

    def test_config_requires_a_finite_positive_interval(self):
        for value in (0, -1, True, '2', float('inf'), float('nan')):
            defaults = default_config()
            defaults['poll_interval_seconds'] = value
            with self.subTest(value=value), patch('wxbridge.config.DEFAULTS', defaults), \
                    nullcontext(), self.assertRaisesRegex(ValueError, 'poll_interval_seconds'):
                load_config('__missing_synthetic_config__.json')
