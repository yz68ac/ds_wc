"""Fault injection for explicit failures, output limits and bounded recovery."""
import copy
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.client import DeepSeekClient, DeepSeekError, ModelResult
import test_native_api as native_tests


class ReviewRecoveryTests(unittest.TestCase):
    def native(self):
        case = native_tests.JournalTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_definite_native_failure_retries_then_releases_queue_with_explicit_error(self):
        case = self.native()
        identifier = case.normal_task()
        job = Mock()
        job.poll.return_value = dict(ok=False, ui=dict(error='native_service_unavailable', operation=dict(submitted=False)))
        factory = Mock(return_value=job)
        for attempt in range(4):
            with patch('wxbridge.app.delivery.time.time', return_value=100+attempt*32):
                case.delivery.send(factory)
                case.delivery.poll_result()
        task = case.journal.task(identifier)
        self.assertEqual(factory.call_count, 4)
        self.assertEqual(task['phase'], 'done')
        self.assertIsNone(task['intent'])
        self.assertEqual(task['delivery_error'], 'native_service_unavailable')
        self.assertEqual(case.journal.get('last_delivery_failure')['submitted'], False)
        self.assertIsNone(case.journal.head())

    def test_unknown_native_result_never_enters_safe_retry_branch(self):
        case = self.native()
        identifier = case.normal_task()
        job = Mock()
        job.poll.return_value = dict(ok=False, ui=dict(error='native_service_unavailable', operation=dict(submitted=None)))
        factory = Mock(return_value=job)
        case.delivery.send(factory)
        case.delivery.poll_result()
        case.delivery.send(factory)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNotNone(case.journal.task(identifier)['intent'])

    def test_truncated_or_empty_model_output_never_becomes_a_successful_chat(self):
        client = DeepSeekClient.__new__(DeepSeekClient)
        for content, finish, error in (('', 'length', 'output_limit'), ('partial', 'length', 'output_limit'),
                                       ('', 'stop', 'empty_reply'), ('', 'content_filter', 'output_filtered')):
            client.request = Mock(return_value=ModelResult(dict(content=content), finish, {}))
            with self.subTest(finish=finish, content=bool(content)), self.assertRaisesRegex(DeepSeekError, error):
                client.chat([])

    def test_tool_calls_may_legitimately_have_no_text(self):
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.request = Mock(return_value=ModelResult(dict(content=None, tool_calls=[dict(id='synthetic')]), 'tool_calls', {}))
        self.assertTrue(client.complete([], require_complete=True)['tool_calls'])
