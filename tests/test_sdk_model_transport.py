"""Exercise real SDK request builders against a synthetic in-memory HTTP server."""
import json
import unittest
from unittest.mock import patch

import anthropic
import httpx2
import openai

from wxbridge.ai.providers import ModelError
from provider.sdk_transport import create_sdk_client, send


class SDKTransportTests(unittest.TestCase):
    def test_protocol_paths_headers_and_bodies(self):
        for provider, sdk, path, payload in (
            ('deepseek', openai, '/v1/chat/completions', {'model':'test','messages':[],'thinking':{'type':'enabled'},'reasoning_effort':'high'}),
            ('openai_compatible', openai, '/v1/chat/completions', {'model':'test','messages':[],'max_tokens':12}),
            ('openai', openai, '/v1/chat/completions', {'model':'test','messages':[],'max_tokens':12}),
            ('openai_responses', openai, '/v1/responses', {'model':'test','input':[],'store':False}),
            ('anthropic', anthropic, '/v1/messages', {'model':'test','messages':[],'max_tokens':12,'temperature':0.5}),
        ):
            with self.subTest(provider=provider):
                calls=[]
                def respond(request):
                    calls.append(request)
                    return httpx2.Response(200, json={'ok':True})
                client=sdk.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
                with patch.object(sdk, 'DefaultHttpxClient', return_value=client) as factory:
                    module, sdk_client = create_sdk_client(provider, 'https://example.test/v1', 'synthetic-key',
                                                          {'proxy_url':'http://127.0.0.1:10808'}, 60)
                    with sdk_client:
                        result = send(module, sdk_client, provider, payload, 4)
                self.assertEqual(result,{'ok':True})
                factory.assert_called_once_with(proxy='http://127.0.0.1:10808')
                self.assertEqual(calls[0].url.path,path)
                self.assertEqual(json.loads(calls[0].content),payload)
                if provider=='anthropic':
                    self.assertEqual(calls[0].headers['authorization'],'Bearer synthetic-key')
                    self.assertNotIn('x-api-key', calls[0].headers)
                    self.assertEqual(calls[0].headers['anthropic-version'],'2026-10-21')
                else:self.assertEqual(calls[0].headers['authorization'],'Bearer synthetic-key')

    def test_http_errors_are_safe_and_sdk_does_not_retry(self):
        for provider,sdk in [('openai_responses',openai),('anthropic',anthropic)]:
            calls=[]
            def respond(request):
                calls.append(request)
                return httpx2.Response(429,json={'error':'PRIVATE'})
            client=sdk.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
            payload={'model':'test','messages':[],'max_tokens':12} if provider=='anthropic' else {'model':'test','input':[]}
            with patch.object(sdk,'DefaultHttpxClient',return_value=client):
                module, sdk_client = create_sdk_client(provider, 'https://example.test/v1', 'key', {'proxy_url':'http://127.0.0.1:1'}, 60)
                with sdk_client:
                    with self.assertRaises(ModelError) as caught:
                        send(module, sdk_client, provider, payload, 4)
            self.assertEqual(caught.exception.code,'http_429')
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn('PRIVATE',str(caught.exception))
            self.assertEqual(len(calls),1)

    def test_anthropic_bearer_uses_explicit_key(self):
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx2.Response(200,json={'ok':True})
        client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(respond))
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY':'unrelated'}), patch.object(anthropic,'DefaultHttpxClient',return_value=client):
            module, sdk_client = create_sdk_client('anthropic', 'https://example.test/v1', 'desired',
                                                   {'proxy_url':'http://127.0.0.1:1','anthropic_auth':'bearer'}, 60)
            with sdk_client:
                send(module, sdk_client, 'anthropic', {'model':'test','messages':[],'max_tokens':12}, 4)
        self.assertEqual(calls[0].headers['authorization'],'Bearer desired')
        self.assertNotIn('x-api-key',calls[0].headers)


if __name__ == '__main__': unittest.main()
