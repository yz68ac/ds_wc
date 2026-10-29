"""Batch participation protocol and model-call boundary; no network or real data."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from provider import ModelError
from wxbridge.ai.participation import evaluate, parse_decision, ParticipationError
from wxbridge.config import DEFAULTS


class DecisionTests(unittest.TestCase):
    def test_selection_is_normalized_to_source_order_and_empty_means_silence(self):
        self.assertEqual(parse_decision('{"reply_to":[12,10]}', [10, 11, 12]), dict(reply_to=[10, 12]))
        self.assertEqual(parse_decision('{"reply_to":[]}', [10, 11]), dict(reply_to=[]))

    def test_old_protocol_extra_fields_and_nonlist_output_are_rejected(self):
        for value in (dict(speak=True, reply_to=1), dict(speak=False, reply_to=None),
                      dict(reply_to=[1], reason='不传推理'), dict(reply_to=[1], text='不许预生成'),
                      dict(reply_to=1), dict(reply_to=None), dict(reply_to='1'), {}, [], True):
            with self.subTest(value=value), self.assertRaises(ParticipationError):
                parse_decision(json.dumps(value, ensure_ascii=False), [1, 2])

    def test_target_ids_must_be_typed_unique_positive_and_source_bound(self):
        for values in ([True], [False], [0], [-1], [1.0], ['1'], [1, 1], [3], [[]], [{}]):
            with self.subTest(values=values), self.assertRaises(ParticipationError):
                parse_decision(json.dumps(dict(reply_to=values)), [1, 2])

    def test_invalid_model_text_has_fixed_error_code_without_content(self):
        for text in ('PRIVATE_MODEL_TEXT', '{"reply_to":[1],"reply_to":[]}',
                     '```json\n{"reply_to":[1]}\n```', '{', 'X'*8193, None):
            with self.subTest(text_type=type(text).__name__):
                try:
                    parse_decision(text, [1])
                except ParticipationError as error:
                    self.assertEqual(error.code, 'participation_invalid_decision')
                    self.assertEqual(str(error), error.code)
                else:
                    self.fail('invalid model output accepted')

    def test_invalid_source_scope_is_rejected_before_using_model_output(self):
        for sources in ([1, 1], [True], [0], None, '1'):
            with self.subTest(sources=sources), self.assertRaises(ParticipationError):
                parse_decision('{"reply_to":[]}', sources)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = deepcopy(DEFAULTS)
        self.prompt = [dict(role='system', content='合成判断规则'), dict(role='user', content='合成候选')]

    def test_evaluate_calls_only_participation_and_uses_shorter_remaining_deadline(self):
        cfg_saved, prompt_saved = deepcopy(self.cfg), deepcopy(self.prompt)
        client = Mock()
        client.complete.return_value = dict(content='{"reply_to":[12,10]}', reasoning_content='PRIVATE_REASONING')
        with patch('wxbridge.ai.client.client_for', return_value=client) as factory, \
             patch('wxbridge.ai.roles.model_settings', return_value=dict(timeout_seconds=20)) as settings, \
             patch('wxbridge.ai.participation.time.monotonic', return_value=100):
            result = evaluate(self.cfg, self.prompt, source_ids=[10, 11, 12], timeout_seconds=5)
        self.assertEqual(result, dict(reply_to=[10, 12]))
        factory.assert_called_once_with('participation')
        settings.assert_called_once_with(self.cfg, 'participation')
        client.complete.assert_called_once_with(self.prompt, deadline=105, require_complete=True)
        self.assertEqual(self.cfg, cfg_saved)
        self.assertEqual(self.prompt, prompt_saved)
        self.assertNotIn('PRIVATE_REASONING', json.dumps(result))

    def test_role_timeout_caps_longer_caller_deadline(self):
        client = Mock()
        client.complete.return_value = dict(content='{"reply_to":[]}')
        with patch('wxbridge.ai.client.client_for', return_value=client), \
             patch('wxbridge.ai.roles.model_settings', return_value=dict(timeout_seconds=20)), \
             patch('wxbridge.ai.participation.time.monotonic', return_value=100):
            self.assertEqual(evaluate(self.cfg, self.prompt, source_ids=[1], timeout_seconds=50), dict(reply_to=[]))
        self.assertEqual(client.complete.call_args.kwargs['deadline'], 120)

    def test_no_candidates_or_elapsed_deadline_cannot_create_a_client(self):
        with patch('wxbridge.ai.client.client_for') as factory, \
             patch('wxbridge.ai.roles.model_settings', return_value=dict(timeout_seconds=20)):
            self.assertEqual(evaluate(self.cfg, self.prompt, source_ids=[]), dict(reply_to=[]))
            with self.assertRaisesRegex(ParticipationError, 'participation_expired'):
                evaluate(self.cfg, self.prompt, source_ids=[1], timeout_seconds=0)
        factory.assert_not_called()

    def test_invalid_or_truncated_completion_is_never_treated_as_silence(self):
        client = Mock()
        with patch('wxbridge.ai.client.client_for', return_value=client), \
             patch('wxbridge.ai.roles.model_settings', return_value=dict(timeout_seconds=20)):
            client.complete.return_value = dict(content='{"reply_to":[999]}')
            with self.assertRaises(ParticipationError):
                evaluate(self.cfg, self.prompt, source_ids=[1])
            client.complete.side_effect = ModelError('output_limit')
            with self.assertRaises(ModelError):
                evaluate(self.cfg, self.prompt, source_ids=[1])


if __name__ == '__main__':
    unittest.main()
