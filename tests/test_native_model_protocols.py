"""Responses/Anthropic contracts and hosted-search fault cases; never network."""
from model_support import default_config
from contextlib import nullcontext
from contextlib import nullcontext
from copy import deepcopy
import json
import os
import re
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from model_support import ClientFixture, ModelError, capture_metrics
from wxbridge.ai.providers import ModelRequest, get_protocol_adapter
from wxbridge.ai.roles import model_settings, validate_models
from wxbridge.ai.search import answer_with_search
from wxbridge.app.metrics import Metrics
from wxbridge.config import DEFAULTS, _deep_merge, load_config

URL='https://example.com/verified'
TOOL=dict(type='function',function=dict(name='web_search',description='synthetic',parameters={'type':'object'}))
CALL=dict(id='call-1',type='function',function=dict(name='web_search',arguments='{"query":"synthetic"}'))
MESSAGES=[dict(role='system',content='PRIVATE_PERSONA'),dict(role='user',content=[
    dict(type='text',text='PRIVATE_CHAT'),dict(type='image_url',image_url=dict(url='data:image/png;base64,AAAA',detail='original'))])]


def config(provider='openai_responses'):
    cfg=default_config()
    cfg['models']['reply']=dict(provider=provider,base_url='https://synthetic.example/v1',api_key_env='SYNTHETIC_KEY',
                              model='synthetic-model',thinking='enabled',reasoning_effort='max',max_tokens=4096)
    return cfg


def client(cfg):
    with nullcontext():return ClientFixture(cfg)


def response(items=None,**values):
    return dict(status='completed',output=items or [dict(type='message',content=[dict(type='output_text',text='合成回答',annotations=[])])],
                usage=dict(input_tokens=10,output_tokens=5,total_tokens=15),**values)


def claude(blocks=None,reason='end_turn',**values):
    return dict(type='message',content=blocks or [dict(type='text',text='合成回答')],stop_reason=reason,
                usage=dict(input_tokens=10,output_tokens=5),**values)


def http(data):
    obj=Mock();obj.__enter__=Mock(return_value=obj);obj.__exit__=Mock(return_value=False)
    obj.read.return_value=json.dumps(data).encode();return obj


class NativeProtocolTests(unittest.TestCase):
    def test_responses_images_functions_temperature_and_stateless_payload(self):
        cfg=config();adapter=get_protocol_adapter('openai_responses');settings=model_settings(cfg)
        wire=adapter.build_payload(settings,ModelRequest(MESSAGES,[TOOL],'auto'))
        self.assertFalse(wire['store']);self.assertIn('reasoning.encrypted_content',wire['include'])
        self.assertEqual(wire['reasoning'],dict(effort='max'));self.assertNotIn('temperature',wire)
        self.assertEqual(wire['max_output_tokens'],4096)
        self.assertEqual(wire['input'][1]['content'][1],dict(type='input_image',image_url='data:image/png;base64,AAAA',detail='original'))
        self.assertEqual(wire['tools'][0]['name'],'web_search');self.assertFalse(wire['tools'][0]['strict'])
        settings['thinking']='disabled'
        wire=adapter.build_payload(settings,ModelRequest(MESSAGES))
        self.assertEqual(wire['reasoning'],dict(effort='none'));self.assertEqual(wire['temperature'],1)
        self.assertNotIn('max_tool_calls',wire)

    def test_responses_reasoning_and_function_items_replayed_once_with_original_ids(self):
        adapter=get_protocol_adapter('openai_responses')
        items=[dict(type='reasoning',id='rs-1',encrypted_content='PRIVATE_STATE',summary=[]),
               dict(type='function_call',id='fc-1',call_id='call-1',name='web_search',arguments='{"query":"synthetic"}')]
        result=adapter.parse_response(response(items));turn=adapter.assistant_message(result.message)
        history=[*MESSAGES,turn,dict(role='tool',tool_call_id='call-1',content='PRIVATE_RESULT')]
        wire=adapter.build_payload(model_settings(config()),ModelRequest(history,[TOOL]))
        self.assertEqual(wire['input'][2:4],items)
        self.assertEqual(wire['input'][-1],dict(type='function_call_output',call_id='call-1',output='PRIVATE_RESULT'))
        turn['_provider_state']['items'][0]['summary'].append('changed')
        self.assertEqual(items[0]['summary'],[]);self.assertEqual(result.message['_provider_state']['items'][0]['summary'],[])
        self.assertNotIn('PRIVATE',repr(result))

    def test_anthropic_system_images_and_sampling_are_converted(self):
        adapter=get_protocol_adapter('anthropic');settings=model_settings(config('anthropic'))
        wire=adapter.build_payload(settings,ModelRequest(MESSAGES,[TOOL]))
        self.assertEqual(wire['system'],'PRIVATE_PERSONA');self.assertEqual(len(wire['messages']),1)
        self.assertEqual(wire['messages'][0]['content'][1],dict(type='image',source=dict(type='base64',media_type='image/png',data='AAAA')))
        self.assertEqual(wire['thinking'],dict(type='adaptive'));self.assertEqual(wire['output_config'],dict(effort='max'))
        self.assertNotIn('temperature',wire);self.assertIn('input_schema',wire['tools'][0])
        settings.update(thinking='disabled',anthropic_version='2026-10-21')
        self.assertNotIn('temperature',adapter.build_payload(settings,ModelRequest(MESSAGES)))
        settings['anthropic_version']='2023-06-01'
        self.assertEqual(adapter.build_payload(settings,ModelRequest(MESSAGES))['temperature'],1)


    def test_anthropic_signed_and_redacted_thinking_survive_parallel_results(self):
        adapter=get_protocol_adapter('anthropic')
        blocks=[dict(type='thinking',thinking='',signature='PRIVATE_SIG'),dict(type='redacted_thinking',data='PRIVATE_REDACTED'),
                dict(type='tool_use',id='call-1',name='web_search',input={'query':'one'}),
                dict(type='tool_use',id='call-2',name='web_search',input={'query':'two'})]
        result=adapter.parse_response(claude(blocks,'tool_use'))
        history=[*MESSAGES,adapter.assistant_message(result.message),
                 dict(role='tool',tool_call_id='call-1',content='first'),dict(role='tool',tool_call_id='call-2',content='second')]
        wire=adapter.build_payload(model_settings(config('anthropic')),ModelRequest(history,[TOOL]))
        self.assertEqual(wire['messages'][1]['content'],blocks)
        self.assertEqual([b['tool_use_id'] for b in wire['messages'][2]['content']],['call-1','call-2'])
        self.assertEqual(result.message['content'],'');self.assertEqual(len(result.message['tool_calls']),2)
        self.assertNotIn('PRIVATE',repr(result))

    def test_manual_thinking_budget_and_between_tools_are_explicit(self):
        settings=model_settings(config('anthropic'));settings.update(thinking_mode='manual',reasoning_effort=None,thinking_budget_tokens=2048)
        adapter=get_protocol_adapter('anthropic');wire=adapter.build_payload(settings,ModelRequest(MESSAGES,[TOOL],'none'))
        self.assertEqual(wire['thinking'],dict(type='enabled',budget_tokens=2048))
        with self.assertRaisesRegex(ModelError,'unsupported_tool_choice'):adapter.build_payload(settings,ModelRequest(MESSAGES,[TOOL],'required'))
        settings['thinking_budget_tokens']=4096
        with self.assertRaisesRegex(ModelError,'invalid_thinking_budget'):adapter.build_payload(settings,ModelRequest(MESSAGES))
        settings.update(thinking='disabled',thinking_mode='between_tools')
        self.assertEqual(adapter.build_payload(settings,ModelRequest(MESSAGES))['thinking']['type'],'between_tools')

    def test_cross_provider_state_and_duplicate_or_unmatched_results_fail_before_http(self):
        for provider in ('openai_responses','anthropic'):
            adapter=get_protocol_adapter(provider);settings=model_settings(config(provider))
            foreign=dict(role='assistant',_provider_state=dict(provider='other',items=[]))
            with self.subTest(provider=provider),self.assertRaisesRegex(ModelError,'continuation_provider_mismatch'):
                adapter.build_payload(settings,ModelRequest([foreign]))
            with self.assertRaisesRegex(ModelError,'invalid_tool_result'):
                adapter.build_payload(settings,ModelRequest([dict(role='tool',tool_call_id='unknown',content='x')]))
            history=[dict(role='assistant',content='',tool_calls=[CALL]),dict(role='tool',tool_call_id='call-1',content='one'),
                     dict(role='tool',tool_call_id='call-1',content='two')]
            with self.assertRaisesRegex(ModelError,'invalid_tool_result'):adapter.build_payload(settings,ModelRequest(history))

    def test_usage_normalization_and_metrics_do_not_double_count_reasoning(self):
        r=response();r['usage'].update(input_tokens_details={'cached_tokens':4},output_tokens_details={'reasoning_tokens':3})
        output=get_protocol_adapter('openai_responses').parse_response(r)
        self.assertEqual(output.usage,dict(prompt_tokens=10,completion_tokens=5,total_tokens=15,cache_read_tokens=4,reasoning_tokens=3))
        a=claude();a['usage'].update(cache_read_input_tokens=4,cache_creation_input_tokens=2)
        usage=get_protocol_adapter('anthropic').parse_response(a).usage
        self.assertEqual(usage['prompt_tokens'],16);self.assertEqual(usage['total_tokens'],21)
        metrics=Metrics();metrics.record_models([dict(role='reply',usage=output.usage)])
        self.assertEqual(metrics.snapshot()['models']['reply']['completion_tokens'],5)
        self.assertEqual(metrics.snapshot()['models']['reply']['reasoning_tokens'],3)

    def test_truncation_refusal_and_malformed_bodies_are_not_complete_answers(self):
        for provider,data in [('openai_responses',dict(response(),status='incomplete',incomplete_details={'reason':'max_output_tokens'})),
                              ('anthropic',claude(reason='max_tokens'))]:
            c=client(config(provider))
            with patch.object(c,'_post',return_value=data),self.assertRaisesRegex(ModelError,'output_limit'):
                c.complete(MESSAGES,require_complete=True)
        for provider,data in [('openai_responses',response([dict(type='message',content=[dict(type='refusal',refusal='PRIVATE')])])),
                              ('anthropic',claude(reason='refusal'))]:
            c=client(config(provider))
            with patch.object(c,'_post',return_value=data),self.assertRaisesRegex(ModelError,'output_filtered'):
                c.complete(MESSAGES,require_complete=True)
        for provider,data in [('openai_responses',dict(status='completed',output=[None])),('anthropic',dict(content=[None],stop_reason='end_turn'))]:
            with self.assertRaisesRegex(ModelError,'invalid_response'):get_protocol_adapter(provider).parse_response(data)



class NativeSearchTests(unittest.TestCase):
    def test_both_native_protocols_can_use_existing_external_search_functions(self):
        protocols=[('openai_responses',response([dict(type='reasoning',id='r1',summary=[],encrypted_content='PRIVATE_STATE'),
                      dict(type='function_call',id='f1',call_id='call-1',name='web_search',arguments='{"query":"synthetic"}')]),response()),
                   ('anthropic',claude([dict(type='thinking',thinking='',signature='PRIVATE_SIGNATURE'),
                      dict(type='tool_use',id='call-1',name='web_search',input={'query':'synthetic'})],'tool_use'),claude())]
        for provider,first,second in protocols:
            with self.subTest(provider=provider):
                cfg=config(provider);c=client(cfg);backend=Mock()
                backend.search.return_value=dict(results=[dict(url=URL,title='来源')])
                with patch.object(c, '_post', side_effect=[first,second]) as send:
                    text,meta=answer_with_search(cfg,c,MESSAGES,search_client=backend)
                self.assertEqual(send.call_count,2);self.assertEqual(meta['calls'],1)
                self.assertNotIn(URL,text);self.assertNotIn('PRIVATE',text)
                wire=send.call_args.args[0]
                if provider=='openai_responses':
                    self.assertEqual(wire['input'][-1]['type'],'function_call_output')
                    self.assertEqual(wire['input'][-3]['encrypted_content'],'PRIVATE_STATE')
                else:
                    self.assertEqual(wire['messages'][-1]['content'][0]['type'],'tool_result')
                    self.assertEqual(wire['messages'][-2]['content'][0]['signature'],'PRIVATE_SIGNATURE')


    def run_search(self,provider,responses,**options):
        cfg=config(provider);cfg['search']['backend']='native';cfg['search'].update(options)
        c=client(cfg)
        self.http=patch.object(c,'_post',side_effect=responses).start()
        self.addCleanup(patch.stopall)
        return answer_with_search(cfg,c,MESSAGES)

    def test_responses_native_search_keeps_evidence_without_adding_citation_links(self):
        data=response([dict(type='web_search_call',id='w1',status='completed',action=dict(type='search',sources=[dict(url=URL)])),
                       dict(type='message',content=[dict(type='output_text',text='查到结果',annotations=[dict(type='url_citation',url=URL,title='来源',end_index=4)])])])
        text,meta=self.run_search('openai_responses',[data])
        wire=self.http.call_args.args[0]
        self.assertEqual(wire['tools'],[{'type':'web_search'}]);self.assertEqual(wire['max_tool_calls'],3)
        self.assertEqual(text,'查到结果');self.assertNotIn('](',text);self.assertNotIn('我去',text)
        self.assertTrue(meta['attempted']);self.assertEqual(meta['calls'],1);self.assertEqual(meta['results'],1)
        self.assertNotIn('PRIVATE',json.dumps(meta))

    def test_anthropic_native_search_200_error_body_is_not_success(self):
        data=claude([dict(type='server_tool_use',id='s1',name='web_search',input={'query':'PRIVATE_QUERY'}),
                     dict(type='web_search_tool_result',tool_use_id='s1',content=dict(type='web_search_tool_result_error',error_code='PRIVATE_ERROR')),
                     dict(type='text',text='错误情况下不该采信的结论')])
        text,meta=self.run_search('anthropic',[data])
        self.assertIn('核实不了',text);self.assertNotIn('不该采信',text)
        self.assertEqual(meta['errors'],['native_search_failed']);self.assertNotIn('PRIVATE',json.dumps(meta))

    def test_anthropic_pause_replays_signed_state_and_reduces_remaining_budget(self):
        first=claude([dict(type='thinking',thinking='PRIVATE_THOUGHT',signature='PRIVATE_SIG'),
                      dict(type='server_tool_use',id='s1',name='web_search',input={'query':'synthetic'})],'pause_turn')
        second=claude([dict(type='web_search_tool_result',tool_use_id='s1',content=[dict(type='web_search_result',url=URL,title='实际来源')]),
                       dict(type='text',text='已核对',citations=[dict(type='web_search_result_location',url=URL,title='实际来源',encrypted_index='PRIVATE_INDEX')])])
        text,meta=self.run_search('anthropic',[first,second])
        self.assertEqual(self.http.call_count,2)
        wire=self.http.call_args.args[0]
        self.assertEqual(wire['messages'][-1]['content'],first['content'])
        self.assertEqual(wire['tools'][0]['max_uses'],2)
        self.assertNotIn(URL,text);self.assertNotIn('PRIVATE',text);self.assertEqual(meta['calls'],1)

    def test_budget_exhaustion_does_not_restart_a_paused_search(self):
        data=claude([dict(type='server_tool_use',id='s1',name='web_search',input={'query':'synthetic'})],'pause_turn')
        text,meta=self.run_search('anthropic',[data],max_requests=1)
        self.assertEqual(self.http.call_count,1);self.assertIn('tool_limit',meta['errors']);self.assertIn('上限',text)

    def test_pause_without_search_is_bounded_by_rounds(self):
        data=claude([dict(type='thinking',thinking='',signature='PRIVATE_SIG')],'pause_turn')
        text,meta=self.run_search('anthropic',[data,data],max_tool_rounds=1)
        self.assertEqual(self.http.call_count,2);self.assertIn('tool_limit',meta['errors']);self.assertNotIn('PRIVATE',text)

    def test_no_search_smalltalk_keeps_normal_reply_and_no_sources(self):
        text,meta=self.run_search('openai_responses',[response()])
        self.assertEqual(text,'合成回答');self.assertFalse(meta['attempted']);self.assertEqual(meta['results'],0)

    def test_private_citations_and_empty_search_results_are_not_presented_as_verified(self):
        data=response([dict(type='web_search_call',id='w1',status='completed',action={'type':'search'}),
                       dict(type='message',content=[dict(type='output_text',text='不要采信',annotations=[dict(type='url_citation',url='http://127.0.0.1/private')])])])
        text,meta=self.run_search('openai_responses',[data])
        self.assertIn('核实不了',text);self.assertNotIn('127.0.0.1',text);self.assertEqual(meta['results'],0)

    def test_excess_server_calls_and_unknown_client_tools_are_rejected(self):
        data=response([dict(type='web_search_call',id='w'+str(i),status='completed',action={'type':'search'}) for i in range(2)])
        with self.assertRaisesRegex(ModelError,'invalid_native_search_result'):
            self.run_search('openai_responses',[data],max_requests=1)
        data=response([dict(type='function_call',call_id='call-1',name='not_allowed',arguments='{}')])
        with self.assertRaisesRegex(ModelError,'unexpected_client_tool'):self.run_search('openai_responses',[data])

    def test_http_failure_does_not_fall_back_to_external_search(self):
        cfg=config();cfg['search']['backend']='native';c=client(cfg)
        with patch.object(c,'_post',side_effect=ModelError('http_429',retryable=True)),patch('wxbridge.ai.search.SearchClient') as external:
            with self.assertRaisesRegex(ModelError,'http_429'):answer_with_search(cfg,c,MESSAGES)
        external.assert_not_called()


class ProfileTests(unittest.TestCase):




    def test_auto_search_is_selected_before_http_and_gateway_can_disable_native_tools(self):
        cfg=config();cfg['search']['backend']='auto';c=client(cfg)
        with patch.object(c,'_post',return_value=response()) as post,patch('wxbridge.ai.search.SearchClient') as external:
            answer_with_search(cfg,c,MESSAGES)
        self.assertEqual(post.call_args.args[0]['tools'],[dict(type='web_search')]);external.assert_not_called()
        cfg['models']['reply']['native_search_supported']=False;c=client(cfg)
        with patch.object(c,'_post',return_value=response()) as post:
            answer_with_search(cfg,c,MESSAGES)
        self.assertEqual(post.call_args.args[0]['tools'][0]['type'],'function')
        cfg['search']['backend']='native'
        with self.assertRaises(ValueError):validate_models(cfg)

    def profile_cfg(self):
        cfg=default_config()
        cfg['model_profiles']={'terra':dict(provider='openai_responses',base_url='https://api.openai.com/v1',
                                          api_key_env='OPENAI_API_KEY',model='gpt-6.1-terra'),
                               'claude':dict(provider='anthropic',base_url='https://api.anthropic.com/v1',
                                           api_key_env='ANTHROPIC_API_KEY',model='synthetic-claude')}
        cfg['model_profile']='terra';return cfg

    def test_global_profile_and_role_override_keep_judgment_disabled(self):
        cfg=self.profile_cfg();cfg['models']['memory']={'profile':'claude','reasoning_effort':'high'}
        original=deepcopy(cfg);validate_models(cfg)
        self.assertEqual(model_settings(cfg,'reply')['model'],'gpt-6.1-terra')
        self.assertEqual(model_settings(cfg,'participation')['thinking'],'disabled')
        self.assertEqual(model_settings(cfg,'memory')['provider'],'anthropic')
        self.assertEqual(model_settings(cfg,'memory')['reasoning_effort'],'high')
        self.assertEqual(cfg,original)

    def test_legacy_configuration_is_unchanged_without_profile_selection(self):
        cfg=default_config();validate_models(cfg)
        self.assertEqual(model_settings(cfg)['provider'],'deepseek')
        self.assertEqual(model_settings(cfg,'memory')['max_tokens'],cfg['memory']['max_tokens'])

    def test_config_loader_accepts_selection_without_reading_env_or_replacing_persona(self):
        cfg=self.profile_cfg()
        with nullcontext(),patch('pathlib.Path.exists',return_value=False):
            loaded=load_config(Path('synthetic-missing.json'))
        self.assertEqual(model_settings(_deep_merge(loaded,cfg))['provider'],'openai_responses')
        self.assertEqual(loaded['deepseek']['system_prompt_file'],DEFAULTS['deepseek']['system_prompt_file'] if not DEFAULTS['deepseek']['system_prompt_file'] else str((Path.cwd()/DEFAULTS['deepseek']['system_prompt_file']).resolve()))
