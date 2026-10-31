"""Gemini uses the existing compatible SDK boundary; all HTTP here is synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx2
import openai

from provider import ModelRequest, create_client, load_config
from wxbridge.ai.roles import model_settings, validate_models
from model_support import default_config


class GeminiConnectionTests(unittest.TestCase):
    def config(self):
        return load_config(Path(__file__).parent / '.synthetic-provider.env', environ={
            'GEMINI_API_KEY': 'synthetic-cpa-key',
            'GEMINI_BASE_URL': 'http://127.0.0.1:8317/v1',
            'GEMINI_PROXY_URL': '',
            'OPENAI_API_KEY': 'synthetic-other-key',
        })

    def test_gateway_connection_is_separate_from_openai_credentials_and_proxy(self):
        config = self.config()
        self.assertEqual(config.protocols['gemini'], 'openai_compatible')
        self.assertEqual(config.api_key('gemini'), 'synthetic-cpa-key')
        self.assertEqual(config.api_key('openai'), 'synthetic-other-key')
        self.assertIsNone(config.connections['gemini'].proxy_url)
        cfg = default_config()
        cfg['model_clients']['reply'] = 'gemini'
        validate_models(cfg)
        self.assertEqual(model_settings(cfg, provider_config=config)['connection'], 'gemini')

    def test_discovery_and_tool_round_trip_reuse_one_sdk_client(self):
        calls = []
        tool = dict(id='synthetic-call', type='function', function=dict(name='lookup', arguments='{}'),
                    extra_content=dict(google=dict(thought_signature='synthetic-signature')))
        def respond(request):
            calls.append(request)
            if request.method == 'GET':
                return httpx2.Response(200, json=dict(object='list', data=[dict(id='gemini-test', object='model')]))
            body = json.loads(request.content)
            message = dict(role='assistant', content='OK') if body['messages'][-1]['role'] == 'tool' else dict(role='assistant', content=None, tool_calls=[tool])
            return httpx2.Response(200, json=dict(id='synthetic', object='chat.completion', created=0,
                model='gemini-test', choices=[dict(index=0, message=message,
                finish_reason='tool_calls' if message.get('tool_calls') else 'stop')]))

        transport = openai.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
        constructor = openai.OpenAI
        with patch.object(openai, 'OpenAI', side_effect=lambda **kw: constructor(**kw, http_client=transport)) as factory:
            with create_client('gemini', config=self.config()) as client:
                self.assertEqual(client.fetch_models()[0].alias, 'gemini/gemini-test')
                messages = [dict(role='user', content='synthetic request')]
                request = ModelRequest(messages, tools=[dict(type='function', function=dict(name='lookup', parameters=dict(type='object')))],
                                       model='gemini-test', options=dict(thinking='enabled', reasoning_effort='medium'))
                first = client.generate(request)
                history = [*deepcopy(messages), client.assistant_message(first.message),
                           dict(role='tool', tool_call_id='synthetic-call', content='synthetic result')]
                result = client.generate(ModelRequest(history, model='gemini-test'))
                self.assertEqual(result.message['content'], 'OK')
            self.assertEqual(factory.call_count, 1)
        self.assertTrue(transport.is_closed)
        self.assertEqual([r.url.path for r in calls], ['/v1/models', '/v1/chat/completions', '/v1/chat/completions'])
        self.assertTrue(all(r.headers['authorization'] == 'Bearer synthetic-cpa-key' for r in calls))
        self.assertEqual(json.loads(calls[1].content)['reasoning_effort'], 'medium')
        self.assertEqual(json.loads(calls[2].content)['messages'][1]['tool_calls'][0]['extra_content'], tool['extra_content'])


if __name__ == '__main__':
    unittest.main()
