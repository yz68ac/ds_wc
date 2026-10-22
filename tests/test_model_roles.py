"""角色隔离、HTTP 错误分类和脱敏元数据；全为合成请求。"""
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch
import urllib.error

from wxbridge.ai.client import DeepSeekClient, DeepSeekError, capture_metrics
from wxbridge.ai.roles import model_settings, validate_models
from wxbridge.config import DEFAULTS


class ModelRoleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)

    def test_legacy_config_and_independent_role_credentials(self):
        self.cfg['models']['participation'] = dict(provider='openai', base_url='https://judge.example/v1',
            api_key_env='SYNTHETIC_JUDGE_KEY', model='synthetic-judge', thinking='disabled', timeout_seconds=4)
        original = copy.deepcopy(self.cfg)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'reply-secret', 'SYNTHETIC_JUDGE_KEY': 'judge-secret'}, clear=True):
            judge = DeepSeekClient(self.cfg, role='participation')
            reply = DeepSeekClient(self.cfg)
        self.assertEqual(judge.api_key, 'judge-secret')
        self.assertEqual(judge.base_url, 'https://judge.example/v1')
        self.assertEqual(reply.api_key, 'reply-secret')
        self.assertEqual(reply.cfg['reasoning_effort'], 'high')
        self.assertEqual(model_settings(self.cfg, 'memory')['reasoning_effort'], 'max')
        self.assertEqual(model_settings(self.cfg, 'avatar')['thinking'], 'disabled')
        self.assertEqual(self.cfg, original)

    def client(self, role='reply'):
        with patch('wxbridge.ai.client.deepseek_api_key', return_value='synthetic-key'):
            return DeepSeekClient(self.cfg, role=role)

    def test_openai_adapter_omits_vendor_thinking_and_uses_role_endpoint(self):
        self.cfg['models']['participation'] = dict(provider='openai', base_url='https://judge.example/v1',
                                                 model='synthetic-judge')
        client = self.client('participation')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
        with patch('wxbridge.ai.client.urllib.request.urlopen', return_value=response) as request:
            self.assertEqual(client.chat([]), 'ok')
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, 'https://judge.example/v1/chat/completions')
        payload = json.loads(req.data)
        self.assertEqual(payload['model'], 'synthetic-judge')
        self.assertNotIn('thinking', payload)
        self.assertNotIn('reasoning_effort', payload)

    def test_permanent_and_transient_errors_are_safe_and_not_locally_retried(self):
        client = self.client()
        for status in (400, 401, 403, 408, 429, 500, 503):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError('https://example.com/PRIVATE', status, 'PRIVATE', {}, None)
                with patch('wxbridge.ai.client.urllib.request.urlopen', side_effect=failure) as post:
                    with self.assertRaises(DeepSeekError) as caught:
                        client.chat([])
                self.assertEqual(post.call_count, 1)
                self.assertEqual(caught.exception.retryable, status in (408, 429) or status >= 500)
                self.assertNotIn('PRIVATE', str(caught.exception))
        with patch('wxbridge.ai.client.urllib.request.urlopen', side_effect=urllib.error.URLError('PRIVATE')):
            with self.assertRaises(DeepSeekError) as caught:
                client.chat([])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), 'connection_failed')

    def test_result_and_metrics_preserve_usage_without_content_or_reasoning(self):
        client = self.client()
        result = dict(choices=[dict(message=dict(content='PRIVATE_REPLY', reasoning_content='PRIVATE_REASONING'),
                                   finish_reason='length')],
                      usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15, private='PRIVATE'))
        with capture_metrics() as metrics, patch.object(client, '_post', return_value=result):
            answer = client.request([dict(role='user', content='PRIVATE_CHAT')])
        self.assertEqual(answer.finish_reason, 'length')
        self.assertEqual(answer.message['content'], 'PRIVATE_REPLY')
        self.assertEqual(answer.usage, dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        self.assertEqual(metrics[0]['usage']['total_tokens'], 15)
        self.assertNotIn('PRIVATE', json.dumps(metrics))

    def test_invalid_role_overrides_are_rejected_before_startup(self):
        for override in ({'provider': 'anthropic'}, {'api_key_env': ''}, {'timeout_seconds': True},
                         {'timeout_seconds': float('nan')}, {'max_tokens': 1.5}, {'thinking': 'auto'},
                         {'base_url': 'https://user:secret@example.com'}, {'extra': 'field'}):
            with self.subTest(override=override):
                self.cfg['models']['reply'] = override
                with self.assertRaises(ValueError):
                    validate_models(self.cfg)

    def test_participation_and_memory_workers_honor_role_deadlines(self):
        from wxbridge.ai.proactive import evaluate
        self.cfg['models']['participation'] = dict(timeout_seconds=5, max_tokens=99)
        client = self.client('participation')
        with patch('wxbridge.ai.client.DeepSeekClient', return_value=client), \
                patch.object(client, '_post', return_value={'choices': [{'message': {'content': '{"speak":false,"text":""}'}}]}) as post:
            self.assertFalse(evaluate(self.cfg, [])['speak'])
        self.assertEqual(post.call_args.args[0]['max_tokens'], 99)
        self.assertLessEqual(post.call_args.args[1], 5)

    def test_worker_returns_safe_error_and_usage_without_sensitive_details(self):
        import logging
        from wxbridge.app.workers import _work
        old = logging.root.manager.disable
        connection = Mock()
        self.cfg['search']['enabled'] = False
        error = urllib.error.HTTPError('https://example.com/PRIVATE', 401, 'PRIVATE', {}, None)
        try:
            with patch('wxbridge.ai.client.deepseek_api_key', return_value='PRIVATE_KEY'), \
                    patch('wxbridge.ai.client.urllib.request.urlopen', side_effect=error):
                _work(connection, 'generate', (self.cfg, [dict(role='user', content='PRIVATE_CHAT')], None))
        finally:
            logging.disable(old)
        result = connection.send.call_args.args[0]
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'http_401')
        self.assertFalse(result['retryable'])
        self.assertEqual(result['model_metrics'][0]['role'], 'reply')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_parent_metrics_only_keep_bounded_roles_and_numeric_usage(self):
        from wxbridge.app.metrics import Metrics
        metrics = Metrics()
        metrics.record_models([dict(role='reply', elapsed_ms=12, usage=dict(prompt_tokens=10, private='PRIVATE'),
                                    content='PRIVATE', error='PRIVATE'), dict(role='PRIVATE', elapsed_ms=1)])
        value = metrics.snapshot()
        self.assertEqual(value['models']['reply']['calls'], 1)
        self.assertEqual(value['models']['reply']['errors'], 1)
        self.assertEqual(value['models']['reply']['prompt_tokens'], 10)
        self.assertNotIn('PRIVATE', json.dumps(value))
