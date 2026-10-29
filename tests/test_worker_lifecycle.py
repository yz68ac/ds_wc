"""Fault injection for scheduling ownership; no real processes, APIs or WeChat."""
from contextlib import ExitStack
import json
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import test_context_pipeline as pipeline
import test_instances as instances
import test_memory as memory_tests
import test_conversation as conversation
from wechat_linux.errors import StoreError
from wxbridge.app.workers import WorkerLease, WorkerCleanupError, ProcessJob
from wxbridge.app.bot import GroupSession


def fixture(case, cls, method):
    value = cls(method)
    value.setUp()
    case.addCleanup(value.doCleanups)
    return value


class WorkerLifecycleTests(unittest.TestCase):
    def test_spawned_generation_is_closed_if_registration_transaction_fails(self):
        f = fixture(self, pipeline.PipelineTests, 'runTest')
        f.add(1)
        f.bot.intake()
        f.bot.participation.step(f.now)
        put, failed = f.bot.journal.put, False

        def fail_registration(task):
            nonlocal failed
            if 'memory_used' in task and not failed:
                failed = True
                raise sqlite3.OperationalError('synthetic write failure')
            return put(task)

        with patch.object(f.bot.journal, 'put', side_effect=fail_registration):
            self.assertEqual(f.bot.start_foreground(), 0)
        self.assertEqual(len(f.jobs), 1)
        self.assertTrue(f.jobs[0].closed)
        self.assertEqual(f.bot.worker_counts()['generation'], 0)
        self.assertEqual(f.bot.journal.task(1)['generation'], 'ready')

    def test_spawned_memory_is_closed_if_registration_transaction_fails(self):
        f = fixture(self, memory_tests.MemoryPipelineTests,
                    'test_memory_after_final_confirmation_runs_alongside_reply_then_recalls_after_restart')
        f.enable()
        f.confirmed()
        f.bot._pause_memory()
        put, failed = f.bot.journal.put_memory, False

        def fail_registration(record):
            nonlocal failed
            if record['state'] == 'running' and not failed:
                failed = True
                raise sqlite3.OperationalError('synthetic write failure')
            return put(record)

        before = len(f.jobs)
        with patch.object(f.bot.journal, 'put_memory', side_effect=fail_registration):
            f.bot.background()
        self.assertEqual(len(f.jobs), before+1)
        self.assertTrue(f.jobs[-1].closed)
        self.assertIsNone(f.bot.memory_job)
        record = f.bot.journal.memory_jobs()[0]
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(record['attempts'], 1)

    def test_worker_lease_closes_on_interrupt_and_transfers_success(self):
        interrupted, retained = Mock(), Mock()
        with self.assertRaises(KeyboardInterrupt):
            with WorkerLease(interrupted):
                raise KeyboardInterrupt()
        interrupted.close.assert_called_once()
        with WorkerLease(retained) as lease:
            self.assertIs(lease.transfer(), retained)
        retained.close.assert_not_called()

    def test_failed_generation_cleanup_propagates_instead_of_admitting_more_work(self):
        f = fixture(self, pipeline.PipelineTests, 'runTest')
        f.add(1)
        f.bot.intake()
        f.bot.participation.step(f.now)
        put = f.bot.journal.put

        def fail_registration(task):
            if 'memory_used' in task:
                raise sqlite3.OperationalError('synthetic registration failure')
            return put(task)

        worker = pipeline.FakeJob('generate', ())
        worker.close = Mock(side_effect=PermissionError('private exception text'))
        with patch.object(f.bot, 'job_factory', return_value=worker), \
             patch.object(f.bot.journal, 'put', side_effect=fail_registration):
            with self.assertRaisesRegex(WorkerCleanupError, '^worker_cleanup_failed$'):
                f.bot.start_foreground()
        self.assertEqual(f.bot.journal.task(1)['generation'], 'running')
        self.assertFalse(f.bot.journal.task(1).get('generation_failed'))
        worker.close.assert_called_once()

    def test_failed_memory_cleanup_propagates_without_normal_retry(self):
        f = fixture(self, memory_tests.MemoryPipelineTests,
                    'test_memory_after_final_confirmation_runs_alongside_reply_then_recalls_after_restart')
        f.enable()
        f.confirmed()
        f.bot._pause_memory()
        worker = pipeline.FakeJob('memory', ())
        worker.close = Mock(side_effect=OSError('private exception text'))
        put = f.bot.journal.put_memory

        def fail_registration(record):
            if record['state'] == 'running':
                raise sqlite3.OperationalError('synthetic registration failure')
            return put(record)

        with patch.object(f.bot, 'job_factory', return_value=worker), \
             patch.object(f.bot.journal, 'put_memory', side_effect=fail_registration):
            with self.assertRaisesRegex(WorkerCleanupError, '^worker_cleanup_failed$'):
                f.bot.background()
        record = f.bot.journal.memory_jobs()[0]
        self.assertEqual((record['state'], record['attempts']), ('pending', 0))
        worker.close.assert_called_once()

    def test_process_close_requires_verified_exit_and_closes_pipe_on_errors(self):
        for terminate_error in (False, True):
            with self.subTest(terminate_error=terminate_error):
                worker = ProcessJob.__new__(ProcessJob)
                worker.process, worker.pipe = Mock(), Mock()
                worker.process.is_alive.return_value = True
                if terminate_error:
                    worker.process.terminate.side_effect = OSError('private exception text')
                with self.assertRaisesRegex(WorkerCleanupError, '^worker_cleanup_failed$'):
                    worker.close()
                worker.pipe.close.assert_called_once()
                if not terminate_error:
                    worker.process.kill.assert_called_once()

    def test_session_close_attempts_every_resource_and_preserves_fatal_error(self):
        bot = GroupSession.__new__(GroupSession)
        bot.native_api, bot.participation, bot.journal = Mock(), Mock(), Mock()
        bot.profile = None
        bot.models = Mock()
        bot._pause_memory = Mock(side_effect=WorkerCleanupError())
        generator, sender = Mock(), Mock()
        bot.generators = {1: (1, generator)}
        bot.delivery = SimpleNamespace(job=(1, 0, sender))
        bot.save_status = Mock()
        with self.assertRaises(WorkerCleanupError):
            bot.close()
        for close in (bot.native_api.close, bot.participation.cancel, bot._pause_memory,
                      generator.close, sender.close, bot.save_status, bot.journal.close):
            close.assert_called_once()

    def test_ready_judgment_is_applied_before_new_real_mention_is_judged(self):
        f = fixture(self, conversation.ConversationTests, 'runTest')
        source = f.ordinary('这事让ds来评评')
        worker = f.bot.participation.job
        worker.result = dict(ok=True, decision=dict(reply_to=[source['local_id']]))
        f.add(source['local_id']+1)
        f.bot.tick()
        self.assertTrue(worker.closed)
        tasks = f.bot.journal.tasks()
        self.assertEqual([t['trigger_ids'] for t in tasks], [[source['local_id']]])
        self.assertEqual(f.bot.participation.pending['source_ids'], [source['local_id']+1])
        f.decision([source['local_id']+1])
        self.assertEqual({t['trigger_kind'] for t in f.bot.journal.tasks()}, {'participation'})
        self.assertEqual(f.bot.worker_counts()['generation'], 2)

    def test_single_group_applies_completed_memory_before_foreground_admission(self):
        f = fixture(self, memory_tests.MemoryPipelineTests,
                    'test_memory_after_final_confirmation_runs_alongside_reply_then_recalls_after_restart')
        f.enable()
        f.confirmed()
        record, value, worker = f.bot.memory_job
        worker.result = dict(ok=True, proposal=memory_tests.suggestion(label='wxid_friend'))
        original = f.bot.memory.apply
        events = []

        def apply(*args):
            events.append('apply')
            return original(*args)

        factory = f.bot.job_factory

        def spawn(kind, *args, **kwargs):
            events.append(kind)
            return factory(kind, *args, **kwargs)

        with patch.object(f.bot.memory, 'apply', side_effect=apply), patch.object(f.bot, 'job_factory', side_effect=spawn):
            f.add(3)
            f.bot.tick()
        self.assertEqual(events[:3], ['apply', 'participation', 'generate'])
        self.assertTrue(worker.closed)
        self.assertEqual(f.bot.journal.memory_jobs(), [])
        self.assertEqual(f.bot.memory.data['revision'], 2)
        self.assertEqual(f.bot.worker_counts()['generation'], 1)

    def test_account_admission_collects_ready_memory_without_retrying_or_rebilling(self):
        f = fixture(self, instances.MultiGroupTests,
                    'test_one_reader_two_independent_cursors_contexts_and_model_slots')
        a, b = f.coordinator.sessions
        record = dict(id=1, state='running', triggers=[dict(local_id=1, time=f.now)], attempts=0, retry_at=0)
        with a.journal.db:
            a.journal.put_memory(record)
        worker = pipeline.FakeJob('memory', ())
        worker.result = dict(ok=True, proposal=dict(synthetic=True))
        memory = Mock()
        memory.data = dict(revision=1)
        memory.status.return_value = {}
        a.memory, a.memory_job = memory, (record, dict(synthetic=True), worker)
        f.add(1, 1)
        b.intake()
        a.collect_background_results()
        memory.apply.assert_called_once_with(worker.result['proposal'], dict(synthetic=True), f.now)
        self.assertTrue(worker.closed)
        self.assertIsNone(a.memory_job)
        self.assertEqual(a.journal.memory_jobs(), [])
        self.assertEqual(record['attempts'], 0)
        self.assertEqual(len(f.jobs), 0)
        a.memory = None

    def test_single_and_multi_group_use_public_phase_surface_in_same_order(self):
        single = fixture(self, pipeline.PipelineTests,
                         'runTest')
        names = ('intake', 'collect_results', 'prioritize', 'start_foreground',
                 'participate', 'deliver', 'background', 'maintain')
        observed = []
        with ExitStack() as stack:
            for name in names:
                stack.enter_context(patch.object(single.bot, name, side_effect=lambda _name=name: observed.append(_name)))
            single.bot.tick()
        self.assertEqual(observed, list(names))
        multi = fixture(self, instances.MultiGroupTests,
                        'test_one_reader_two_independent_cursors_contexts_and_model_slots')
        observed = []
        with ExitStack() as stack:
            for session in multi.coordinator.sessions:
                for name in names:
                    def phase(*args, _name=name, _id=session.instance_id, **kwargs):
                        observed.append((_id, _name))
                        return 0
                    stack.enter_context(patch.object(session, name, side_effect=phase))
            multi.coordinator.tick()
        self.assertLess(max(i for i, (_, phase) in enumerate(observed) if phase == 'collect_results'),
                        min(i for i, (_, phase) in enumerate(observed) if phase == 'start_foreground'))
        for identifier in ('alpha', 'beta'):
            self.assertEqual([phase for uid, phase in observed if uid == identifier], list(names))

    def test_one_group_diagnostic_write_failure_does_not_take_down_other_groups(self):
        f = fixture(self, instances.MultiGroupTests,
                    'test_one_reader_two_independent_cursors_contexts_and_model_slots')
        a, b = f.coordinator.sessions
        replace = os.replace

        def fail_one(source, target):
            if target == a.status_path:
                raise PermissionError('synthetic diagnostic failure')
            return replace(source, target)

        f.add(0, 1)
        f.add(1, 1)
        with patch('wxbridge.app.bot.os.replace', side_effect=fail_one):
            f.coordinator.tick()
        self.assertEqual((a.last_local_id, b.last_local_id), (1, 1))
        self.assertEqual(a.status_write_error, 'PermissionError')
        self.assertEqual(len(f.active('generate')), 2)
        status = json.loads(f.coordinator.status_path.read_text(encoding='utf-8'))
        self.assertEqual(status['instances']['alpha']['status_write_error'], 'PermissionError')
        self.assertEqual(status['instances']['beta']['status_write_error'], '')
        a.save_status()
        self.assertEqual(a.status_write_error, '')

    def test_shared_reader_and_queue_failures_still_fail_closed(self):
        f = fixture(self, instances.MultiGroupTests,
                    'test_one_reader_two_independent_cursors_contexts_and_model_slots')
        f.add(0, 1)
        f.add(1, 1)
        with patch.object(f.store, 'messages_after', side_effect=StoreError('synthetic reader failure')):
            with self.assertRaises(StoreError):
                f.coordinator.tick()
        self.assertFalse(f.active('native_send'))
        with patch.object(f.coordinator.sessions[0].journal, 'intake_participation', side_effect=sqlite3.OperationalError('synthetic queue failure')):
            with self.assertRaises(sqlite3.OperationalError):
                f.coordinator.tick()
        self.assertFalse(f.active('native_send'))

    def test_unknown_status_errors_are_not_silently_downgraded(self):
        f = fixture(self, pipeline.PipelineTests,
                    'runTest')
        with patch('wxbridge.app.bot.os.replace', side_effect=ValueError('synthetic unexpected failure')):
            with self.assertRaises(ValueError):
                f.bot.save_status()


if __name__ == '__main__':
    unittest.main()
