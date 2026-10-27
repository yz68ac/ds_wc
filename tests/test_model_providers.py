"""Provider boundary contract tests; synthetic HTTP and tools only."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.client import DeepSeekClient, DeepSeekError, ModelClient, ModelError, capture_metrics
from wxbridge.ai.providers import ModelRequest, ModelResult, get_adapter
from wxbridge.ai.roles import validate_models
from wxbridge.ai.search import SearchClient, SearchError, answer_with_search
from wxbridge.config import DEFAULTS

ROOT=Path(__file__).resolve().parents[1]


class SyntheticWireAdapter:
    """A deliberately different protocol, not an implementation of any vendor."""
    def endpoint(self, base_url):
        return base_url+'/synthetic/generate'

    def headers(self, key, settings=None):
        return {'Content-Type':'application/json','X-Synthetic-Key':key}

    def encode(self, settings, request):
        # The tool loop is unaware of the provider's continuation token format.
        turns=[]
        for message in request.messages:
            turn=deepcopy(message)
            if '_continuation' in turn:
                turn['wire_state']=turn.pop('_continuation')
            turns.append(turn)
        return dict(engine=settings['model'],turns=turns,functions=request.tools,choice=request.tool_choice)

    def decode(self, data):
        message=dict(content=data['answer'],tool_calls=data.get('operations',[]))
        if 'wire_state' in data:message['_continuation']=data['wire_state']
        return ModelResult(message,'tool_calls' if message['tool_calls'] else 'stop',dict(total_tokens=7))

    def assistant_message(self, message):
        return {'role':'assistant',**deepcopy(message)}


def response(data):
    obj=Mock();obj.__enter__=Mock(return_value=obj);obj.__exit__=Mock(return_value=False)
    obj.read.return_value=json.dumps(data).encode()
    return obj


class ProviderTests(unittest.TestCase):
    def client(self, cfg=None, **options):
        with patch('wxbridge.ai.client.deepseek_api_key',return_value='PRIVATE_KEY'):
            return ModelClient(cfg or deepcopy(DEFAULTS),**options)

    def test_legacy_names_are_identity_aliases_not_a_second_implementation(self):
        self.assertIs(DeepSeekClient,ModelClient);self.assertIs(DeepSeekError,ModelError)

    def test_chat_completions_alias_validates_and_role_key_is_independent(self):
        cfg=deepcopy(DEFAULTS)
        cfg['models']['reply']=dict(provider='openai_compatible',base_url='https://synthetic.example/v1',
                                   api_key_env='SYNTHETIC_OTHER_KEY',model='synthetic-model',thinking='disabled',reasoning_effort=None)
        validate_models(cfg)
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'original-key','SYNTHETIC_OTHER_KEY':'other-key'},clear=True):
            client=ModelClient(cfg);memory=ModelClient(cfg,role='memory')
        self.assertEqual(client.api_key,'other-key');self.assertEqual(memory.api_key,'original-key')
        self.assertEqual(client.adapter.endpoint(client.base_url),'https://synthetic.example/v1/chat/completions')

    def test_provider_payloads_keep_images_tools_and_do_not_mutate_inputs(self):
        messages=[dict(role='user',content=[dict(type='text',text='PRIVATE_CHAT'),
                  dict(type='image_url',image_url=dict(url='data:image/png;base64,AAAA',detail='original'))])]
        tools=[dict(type='function',function=dict(name='lookup',parameters={'type':'object'}))]
        original=deepcopy((messages,tools));settings=deepcopy(DEFAULTS['deepseek'])
        for provider in ('deepseek','openai','openai_compatible'):
            adapter=get_adapter(provider);wire=adapter.encode(settings,ModelRequest(messages,tools,'none'))
            self.assertEqual(wire['messages'],messages);self.assertEqual(wire['tools'],tools)
            self.assertEqual(wire['tool_choice'],'none')
            self.assertEqual('thinking' in wire,provider=='deepseek')
            wire['messages'][0]['content'].clear();wire['tools'].clear()
            self.assertEqual((messages,tools),original)

    def test_alternative_wire_protocol_can_run_the_existing_search_loop(self):
        cfg=deepcopy(DEFAULTS);cfg['search']['backend']='auto'
        client=self.client(cfg,adapter=SyntheticWireAdapter())
        tool=dict(id='call-1',type='function',function=dict(name='web_search',arguments='{"query":"synthetic"}'))
        replies=[response(dict(answer='',operations=[tool],wire_state='PRIVATE_CONTINUATION')),
                 response(dict(answer='合成结论'))]
        backend=Mock();backend.search.return_value=dict(results=[dict(url='https://example.com/source',title='合成来源')])
        with patch('wxbridge.ai.client.urllib.request.urlopen',side_effect=replies) as http,capture_metrics() as metrics:
            text,meta=answer_with_search(cfg,client,[dict(role='user',content='PRIVATE_CHAT')],search_client=backend)
        self.assertEqual(http.call_count,2);self.assertTrue(meta['attempted'])
        self.assertTrue(text.startswith('合成结论'));self.assertNotIn('PRIVATE',text)
        second=json.loads(http.call_args_list[1].args[0].data)
        self.assertEqual(second['turns'][-2]['wire_state'],'PRIVATE_CONTINUATION')
        self.assertEqual(second['turns'][-1]['tool_call_id'],'call-1')
        self.assertNotIn('messages',second);self.assertNotIn('thinking',second)
        for call in http.call_args_list:
            req=call.args[0]
            self.assertTrue(req.full_url.endswith('/synthetic/generate'))
            self.assertEqual(req.get_header('X-synthetic-key'),'PRIVATE_KEY')
            self.assertIsNone(req.get_header('Authorization'))
        self.assertNotIn('PRIVATE',json.dumps(metrics))

    def test_deepseek_tool_continuation_keeps_hidden_state_without_mutation(self):
        message=dict(content=None,tool_calls=[{'id':'synthetic'}],reasoning_content='PRIVATE_REASONING')
        turn=self.client().assistant_message(message)
        self.assertEqual(turn['reasoning_content'],'PRIVATE_REASONING');self.assertEqual(turn['role'],'assistant')
        turn['tool_calls'].clear();self.assertEqual(message['tool_calls'],[{'id':'synthetic'}])

    def test_expired_budget_after_encoding_never_starts_http(self):
        adapter=SyntheticWireAdapter();client=self.client(adapter=adapter)
        with patch('wxbridge.ai.client.time.monotonic',side_effect=[1,2,4,4]),patch.object(client,'_post') as post:
            with self.assertRaisesRegex(ModelError,'deadline_exceeded'):client.request([],deadline=3)
        post.assert_not_called()

    def test_malformed_content_is_safe_and_request_result_repr_hide_bodies(self):
        adapter=get_adapter('deepseek')
        for message in ({'content':{'PRIVATE':'body'}},{'content':['PRIVATE']},{'tool_calls':{'PRIVATE':'tool'}}):
            with self.subTest(message=message),self.assertRaisesRegex(ModelError,'invalid_response'):
                adapter.decode(dict(choices=[dict(message=message)]))
        request=ModelRequest([dict(content='PRIVATE_CHAT')])
        result=adapter.decode(dict(choices=[dict(message=dict(content='PRIVATE_REPLY',reasoning_content='PRIVATE_REASONING'))],
                                   usage=dict(prompt_tokens=True,completion_tokens=-1,total_tokens=3,extra='PRIVATE')))
        self.assertEqual(result.usage,dict(total_tokens=3))
        self.assertNotIn('PRIVATE',repr(request));self.assertNotIn('PRIVATE',repr(result))

    def test_unknown_provider_fails_before_credentials_or_http(self):
        cfg=deepcopy(DEFAULTS);cfg['models']['reply']['provider']='not-implemented'
        with patch('wxbridge.ai.client.deepseek_api_key') as key,patch('wxbridge.ai.client.urllib.request.urlopen') as http:
            with self.assertRaisesRegex(ModelError,'unsupported_provider'):ModelClient(cfg)
        key.assert_not_called();http.assert_not_called()

    def test_explicit_search_key_never_uses_the_replacement_chat_key(self):
        cfg=deepcopy(DEFAULTS);cfg['search']['api_key_env']='SYNTHETIC_SEARCH_KEY'
        cfg['deepseek']['api_key_env']='SYNTHETIC_NEW_CHAT_KEY'
        with patch.dict(os.environ,{'SYNTHETIC_SEARCH_KEY':'search-key','SYNTHETIC_NEW_CHAT_KEY':'chat-key'},clear=True):
            self.assertEqual(SearchClient(cfg).api_key,'search-key')
        with patch.dict(os.environ,{'SYNTHETIC_NEW_CHAT_KEY':'chat-key'},clear=True):
            with self.assertRaisesRegex(SearchError,'missing_api_key'):SearchClient(cfg)
        cfg['search']['api_key_env']=''
        with self.assertRaisesRegex(SearchError,'invalid_api_key_env'):SearchClient(cfg)

    def test_provider_import_is_isolated_from_bot_config_and_runtime(self):
        code='''
import sys
sys.path.insert(0,sys.argv[1])
from wxbridge.ai.providers import get_adapter, ModelRequest
assert 'wxbridge.config' not in sys.modules
assert 'wxbridge.ai.client' not in sys.modules
assert not any(n.startswith(('frida','gi','wechat_linux')) for n in sys.modules)
assert get_adapter('deepseek').endpoint('https://example.invalid')
'''
        run=subprocess.run([sys.executable,'-I','-B','-c',code,str(ROOT)],capture_output=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr.decode())

    def test_business_entrypoints_use_neutral_names_and_search_has_no_vendor_replay(self):
        for name in ('wxbridge/ai/avatars.py','wxbridge/ai/memory.py','wxbridge/ai/proactive.py','wxbridge/app/workers.py'):
            text=(ROOT/name).read_text(encoding='utf-8')
            self.assertNotIn('DeepSeekClient',text);self.assertNotIn('DeepSeekError',text)
        self.assertNotIn('reasoning_content',(ROOT/'wxbridge/ai/search.py').read_text(encoding='utf-8'))
