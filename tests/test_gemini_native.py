"""Native Gemini wire, signed tool replay, media and failure boundaries; no live HTTP."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
from google import genai

from provider import Client, ImageRequest, ModelError, ModelRequest, capture_metrics, create_client, load_config
from provider.adapters import get_protocol_adapter
from wxbridge.ai.roles import validate_models
from wxbridge.ai.search import answer_with_search
from model_support import default_config


TOOL = dict(type='function', function=dict(name='lookup', description='synthetic lookup',
            parameters=dict(type='object', properties=dict(query=dict(type='string')),
                            required=['query'], additionalProperties=False)))
SIGNATURE = base64.b64encode(b'PRIVATE_SIGNATURE').decode()


def response(parts=None, reason='STOP', **extra):
    return dict(candidates=[dict(content=dict(role='model', parts=parts or [dict(text='OK')]),
                                finishReason=reason)], **extra)


def tool_response(name='lookup', *, identifier=None, query='synthetic'):
    function = dict(name=name, args=dict(query=query))
    if identifier is not None:function['id'] = identifier
    return response([dict(text='PRIVATE_THOUGHT', thought=True),
                     dict(functionCall=function, thoughtSignature=SIGNATURE)])


class GeminiNativeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = get_protocol_adapter('gemini')
        self.settings = dict(model='gemini-test', thinking='enabled', reasoning_effort='high',
                             max_tokens=None, temperature=None)

    def config(self, **overrides):
        return load_config(Path(__file__).parent/'.synthetic-provider.env', environ={
            'GEMINI_API_KEY':'synthetic-cpa-key', 'GEMINI_BASE_URL':'https://gateway.example', **overrides})

    def test_native_is_the_default_and_image_url_can_share_the_connection(self):
        cfg = self.config()
        self.assertEqual(cfg.protocols['gemini'], 'gemini')
        self.assertEqual(cfg.options['gemini']['image_base_url'], 'https://gateway.example')
        for bad in ('https://gateway.example/v1','https://gateway.example/v1beta','https://gateway.example/v1beta/openai'):
            with self.assertRaisesRegex(ValueError,'gemini_base_url_requires_server_root'):
                self.config(GEMINI_BASE_URL=bad)
        bot = default_config();bot['model_clients']['reply'] = 'gemini'
        bot['models']['reply']['options']['max_tokens'] = None
        with patch('wxbridge.ai.roles.load_config', return_value=cfg):validate_models(bot)
        with create_client('gemini', config=cfg) as client:
            with patch.object(client, '_post', return_value=response()) as post:
                self.assertEqual(client.generate(ModelRequest([dict(role='user',content='hi')],
                    model='gemini-test', options=dict(max_tokens=None)), require_complete=True).message['content'], 'OK')
                self.assertNotIn('max_output_tokens',post.call_args.args[0]['config'])

    def test_real_sdk_discovery_and_signed_tool_loop_share_one_client(self):
        calls, created = [], []
        constructor = genai.Client
        def respond(request):
            calls.append(request)
            if request.method == 'GET':
                if request.url.params.get('pageToken'):
                    return httpx.Response(200,json=dict(models=[dict(name='models/second', displayName='Second')]))
                return httpx.Response(200,json=dict(models=[dict(name='models/gemini-test', displayName='Test')],nextPageToken='next'))
            body = json.loads(request.content)
            last = body['contents'][-1]['parts'][0]
            return httpx.Response(200,json=response() if 'functionResponse' in last else tool_response())
        def make(**kwargs):
            self.assertFalse(kwargs['http_options']['client_args']['trust_env'])
            kwargs['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            client = constructor(**kwargs);created.append(client);return client
        with patch.object(genai,'Client',side_effect=make) as factory:
            with create_client('gemini',config=self.config()) as client:
                self.assertEqual([m.id for m in client.fetch_models()], ['gemini-test','second'])
                first = client.generate(ModelRequest([dict(role='user',content='hi')], tools=[TOOL],
                    model='gemini-test', options=dict(thinking='enabled',reasoning_effort='high')),require_complete=True)
                self.assertEqual(first.finish_reason,'tool_calls');self.assertEqual(first.native_finish_reason,'STOP')
                self.assertEqual(first.message['content'],'')
                identifier = first.message['tool_calls'][0]['id']
                history = [dict(role='user',content='hi'),client.assistant_message(first.message),
                           dict(role='tool',tool_call_id=identifier,content='{"answer":"synthetic"}')]
                snapshot = deepcopy(history)
                final = client.generate(ModelRequest(history,tools=[TOOL],model='gemini-test'),require_complete=True)
                self.assertEqual(final.message['content'],'OK');self.assertEqual(history,snapshot)
                self.assertEqual(factory.call_count,1)
                with self.assertRaisesRegex(ModelError,'continuation_provider_mismatch'):
                    client.generate(ModelRequest(history,model='different-model'))
        self.assertTrue(created[0]._api_client._httpx_client.is_closed)
        self.assertEqual([r.url.path for r in calls],['/v1beta/models','/v1beta/models',
                         '/v1beta/models/gemini-test:generateContent','/v1beta/models/gemini-test:generateContent'])
        self.assertTrue(all(r.headers['x-goog-api-key']=='synthetic-cpa-key' for r in calls))
        first, second = [json.loads(r.content) for r in calls if r.method=='POST']
        self.assertEqual(first['generationConfig']['thinkingConfig']['thinking_level'],'HIGH')
        self.assertEqual(first['toolConfig']['functionCallingConfig']['mode'],'AUTO')
        self.assertEqual(first['tools'][0]['functionDeclarations'][0]['name'],'lookup')
        model_parts = second['contents'][1]['parts']
        self.assertEqual(model_parts[1]['thoughtSignature'],SIGNATURE)
        self.assertEqual(model_parts[0]['text'],'PRIVATE_THOUGHT')
        self.assertEqual(second['contents'][2]['parts'][0]['functionResponse'],dict(name='lookup',response=dict(answer='synthetic')))
        self.assertNotIn('gemini_call_',json.dumps(second))

    def test_images_system_prompt_and_tool_choices_are_native_and_inputs_unchanged(self):
        messages = [dict(role='system',content='PRIVATE_SYSTEM'),dict(role='developer',content='policy'),
            dict(role='user',content=[dict(type='text',text='look'),dict(type='image_url',image_url=dict(url='data:image/png;base64,YWJj'))])]
        snapshot = deepcopy(messages)
        for choice, expected in [('auto','AUTO'),('none','NONE'),('required','ANY'),
                                 (dict(type='function',function=dict(name='lookup')),'ANY')]:
            wire = self.adapter.build_payload(self.settings,ModelRequest(messages,tools=[TOOL],tool_choice=choice))
            self.assertEqual(wire['config']['tool_config']['function_calling_config']['mode'],expected)
            self.assertNotIn('max_output_tokens',wire['config'])
            self.assertEqual(wire['contents'][0]['parts'][1]['inline_data'],dict(mime_type='image/png',data='YWJj'))
            self.assertEqual(wire['config']['system_instruction']['parts'][0]['text'],'PRIVATE_SYSTEM\n\npolicy')
        self.assertEqual(messages,snapshot)
        for bad in ['data:image/png;base64,!', 'https://example.com/image.png']:
            messages[-1]['content'][-1]['image_url']['url']=bad
            with self.assertRaisesRegex(ModelError,'invalid_image_input'):
                self.adapter.build_payload(self.settings,ModelRequest(messages))

    def test_native_tool_arguments_and_ids_are_validated_before_execution(self):
        for function in ('PRIVATE',[],dict(name='lookup',args='PRIVATE'),dict(name='',args={}),
                         dict(name='lookup',args=[]),dict(name='lookup',args={},id=123),
                         dict(name='lookup',args={'bad':float('nan')})):
            with self.subTest(function=function),self.assertRaises(ModelError) as caught:
                self.adapter.parse_response(response([dict(functionCall=function)]))
            self.assertNotIn('PRIVATE',str(caught.exception))
        with self.assertRaisesRegex(ModelError,'invalid_response'):
            self.adapter.parse_response(response([dict(functionCall=dict(id='duplicate',name='x',args={})),
                                                 dict(functionCall=dict(id='duplicate',name='y',args={}))]))

    def test_parallel_results_are_bound_and_ordered_and_native_ids_preserved(self):
        first = self.adapter.parse_response(response([dict(functionCall=dict(id='a',name='first',args={})),
                                                     dict(functionCall=dict(id='b',name='second',args={}))]))
        history = [dict(role='user',content='hi'),self.adapter.assistant_message(first.message),
                   dict(role='tool',tool_call_id='b',content='second'),dict(role='tool',tool_call_id='a',content='{"value":1}')]
        wire = self.adapter.build_payload(self.settings,ModelRequest(history))
        self.assertEqual([p['function_response']['id'] for p in wire['contents'][-1]['parts']],['a','b'])
        self.assertEqual(wire['contents'][-1]['parts'][0]['function_response']['response'],{'value':1})
        for bad in (history[:-1],history+history[-1:],history[:2]+[dict(role='tool',tool_call_id='missing',content='x')]):
            with self.assertRaisesRegex(ModelError,'invalid_tool_result'):
                self.adapter.build_payload(self.settings,ModelRequest(bad))
        history[1]['tool_calls'][0]['function']['name']='tampered'
        with self.assertRaisesRegex(ModelError,'invalid_tool_result'):
            self.adapter.build_payload(self.settings,ModelRequest(history))

    def test_finish_reasons_and_usage_keep_native_diagnostics_without_thought_text(self):
        for native, finish in [('SAFETY','content_filter'),('RECITATION','content_filter'),('MAX_TOKENS','length'),
                              ('MALFORMED_FUNCTION_CALL','invalid_tool_calls'),('OTHER','incomplete_response'),
                              ('FUTURE_REASON','incomplete_response')]:
            result=self.adapter.parse_response(response(reason=native))
            self.assertEqual((result.finish_reason,result.native_finish_reason),(finish,native))
            with self.assertRaises(ModelError):Client.check_complete(result)
        blocked=self.adapter.parse_response(dict(promptFeedback=dict(blockReason='PROHIBITED_CONTENT')))
        self.assertEqual((blocked.finish_reason,blocked.native_finish_reason),('content_filter','PROHIBITED_CONTENT'))
        result=self.adapter.parse_response(response([dict(thought=True,text='PRIVATE'),dict(text='visible')],
            usageMetadata=dict(promptTokenCount=10,candidatesTokenCount=3,thoughtsTokenCount=2,cachedContentTokenCount=4,totalTokenCount=15)))
        self.assertEqual(result.message['content'],'visible')
        self.assertEqual(result.usage,dict(prompt_tokens=10,completion_tokens=5,reasoning_tokens=2,cache_read_tokens=4,total_tokens=15))
        self.assertNotIn('PRIVATE',repr(result))
        with Client(dict(provider='gemini',model='gemini-test',base_url='https://example.test'),api_key='synthetic') as client:
            with patch.object(client,'_post',return_value=response(reason='SAFETY')),capture_metrics() as metrics:
                with self.assertRaisesRegex(ModelError,'output_filtered'):client.request([],require_complete=True)
            self.assertEqual(metrics[0]['native_finish_reason'],'SAFETY')
        from wxbridge.app.metrics import Metrics
        counter=Metrics();counter.record_models(metrics)
        self.assertEqual(counter.snapshot()['models']['reply']['last_native_finish_reason'],'SAFETY')

    def test_thinking_budget_and_unsupported_effort_are_explicit(self):
        request=ModelRequest([dict(role='user',content='hi')])
        config=self.adapter.build_payload(dict(model='synthetic',thinking='disabled'),request)['config']
        self.assertEqual(config['thinking_config'],dict(thinking_budget=0))
        config=self.adapter.build_payload(dict(model='synthetic',thinking='enabled',thinking_mode='manual',thinking_budget_tokens=2048),request)['config']
        self.assertEqual(config['thinking_config'],dict(thinking_budget=2048))
        for effort in ('xhigh','max'):
            with self.assertRaisesRegex(ModelError,'unsupported_reasoning_effort'):
                self.adapter.build_payload(dict(self.settings,reasoning_effort=effort),request)

    def test_existing_external_search_loop_uses_native_function_responses(self):
        cfg=default_config();cfg['search']['backend']='external'
        backend=Mock();backend.search.return_value=dict(results=[dict(url='https://example.com/source',title='synthetic')])
        with Client(dict(provider='gemini',model='gemini-test',base_url='https://example.test'),api_key='synthetic') as client:
            with patch.object(client,'_post',side_effect=[tool_response('web_search'),response([dict(text='合成结论')])]) as post:
                text,info=answer_with_search(cfg,client,[dict(role='user',content='synthetic')],search_client=backend)
        self.assertIn('合成结论',text);self.assertTrue(info['attempted']);self.assertEqual(info['calls'],1)
        self.assertEqual(post.call_args.args[0]['contents'][-1]['parts'][0]['function_response']['name'],'web_search')

    def test_http_errors_do_not_retry_or_expose_provider_bodies(self):
        for status in (401,429,503):
            calls=[];constructor=genai.Client
            def respond(request):
                calls.append(request);return httpx.Response(status,json=dict(error=dict(code=status,message='PRIVATE')))
            def make(**kw):
                kw['http_options']['client_args']['transport']=httpx.MockTransport(respond);return constructor(**kw)
            with patch.object(genai,'Client',side_effect=make),create_client('gemini',config=self.config()) as client:
                with self.assertRaises(ModelError) as error:
                    client.generate(ModelRequest([dict(role='user',content='hi')],model='gemini-test'))
            self.assertEqual(error.exception.code,'http_'+str(status));self.assertNotIn('PRIVATE',str(error.exception))
            self.assertEqual(error.exception.retryable,status!=401);self.assertEqual(len(calls),1)

    def test_text_and_images_reuse_the_native_client_when_endpoint_matches(self):
        calls=[];constructor=genai.Client;data=base64.b64encode(b'synthetic-image').decode()
        def respond(request):
            body=json.loads(request.content);calls.append(body)
            if body.get('generationConfig',{}).get('responseModalities'):
                return httpx.Response(200,json=response([dict(inlineData=dict(mimeType='image/png',data=data))]))
            return httpx.Response(200,json=response())
        def make(**kw):
            kw['http_options']['client_args']['transport']=httpx.MockTransport(respond);return constructor(**kw)
        with patch.object(genai,'Client',side_effect=make) as factory,create_client('gemini',config=self.config()) as client:
            client.generate(ModelRequest([dict(role='user',content='hi')],model='gemini-test'))
            self.assertEqual(client.generate_images(ImageRequest('image-test','synthetic')).images[0].data,b'synthetic-image')
            self.assertEqual(factory.call_count,1)


if __name__=='__main__':unittest.main()
