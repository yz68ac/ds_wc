"""Mixed tools, request isolation and hosted-search replay; no network or UI."""
from wxbridge.contracts.image_jobs import ImageProposal
from tests.support.configuration import replace_config, update_config, snapshot
from concurrent.futures import CancelledError
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import Mock, patch

from provider import Client, ModelError, ModelRequest, ModelResult, capture_metrics
from provider.adapters import get_protocol_adapter
from tests.support.model_support import default_config
from tests.support.tool_support import model_result
from wxbridge.ai.reply.runner import run_tool_loop
from wxbridge.ai.tools.registry import get_enabled_tools, apply_tool_instructions
from wxbridge.ai.tools.image import IMAGE_TOOL
from wxbridge.ai.tools.base import ToolError, ToolResult


def call(identifier, name, **arguments):
    return dict(id=identifier, type='function', function=dict(name=name, arguments=json.dumps(arguments)))


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', True, owner=self)
        self.messages = [dict(role='user', content='PRIVATE_CHAT')]
        self.client = Mock()
        self.client.check_complete.side_effect = Client.check_complete
        self.client.assistant_message.side_effect = get_protocol_adapter('deepseek').assistant_message
        self.backend = Mock()
        self.backend.search.return_value = dict(results=[dict(url='https://example.com/source', summary='synthetic')])
        self.backend.fetch.return_value = dict(content='synthetic page')

    def run_reply(self, **kwargs):
        return run_tool_loop(self.cfg, self.client, self.messages, search_client=self.backend, **kwargs)

    def test_registered_policies_match_enabled_tools_and_preserve_input(self):
        original=[dict(role='system',content='synthetic'),dict(role='user',content='private question')]
        saved=deepcopy(original)
        for search_enabled,image_enabled in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(search=search_enabled,image=image_enabled):
                cfg=deepcopy(self.cfg)
                cfg = replace_config(cfg, 'search.enabled', search_enabled)
                cfg = replace_config(cfg, 'image_generation.enabled', image_enabled)
                tools=get_enabled_tools(cfg,search_backend='external')
                once=apply_tool_instructions(cfg,original,tools=tools)
                twice=apply_tool_instructions(cfg,once,tools=tools)
                text=twice[0]['content']
                expected=set()
                if search_enabled:expected.add('search')
                if image_enabled:expected.add('image_generation')
                parts=getattr(twice,'system_parts',{})
                self.assertEqual(set(parts),expected)
                for policy in parts.values():
                    self.assertTrue(policy.strip())
                    self.assertEqual(text.count(policy),1)
                self.assertEqual(once,twice)
                self.assertEqual(original,saved)

    def test_native_search_policy_is_included_without_a_client_search_tool(self):
        cfg=deepcopy(self.cfg);cfg = replace_config(cfg, 'image_generation.enabled', False)
        tools=get_enabled_tools(cfg,search_backend='native')
        self.assertEqual(tools,[])
        messages=apply_tool_instructions(cfg,self.messages,tools=tools)
        self.assertEqual(set(messages.system_parts),{'search'})

    def test_disabled_search_uses_tool_timeout_and_honors_caller_deadline(self):
        self.cfg = update_config(self.cfg, 'search', enabled=False, total_timeout_seconds=1, owner=self)
        self.cfg = replace_config(self.cfg, 'models.reply.options.timeout_seconds', 240, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 200, owner=self)
        self.client.request.return_value=model_result(dict(content='普通回复'))
        with patch('wxbridge.ai.reply.runner.time.monotonic',return_value=100):
            self.run_reply()
        self.assertEqual(self.client.request.call_args.kwargs['deadline'],300)
        with patch('wxbridge.ai.reply.runner.time.monotonic',return_value=100):
            self.run_reply(deadline=180)
        self.assertEqual(self.client.request.call_args.kwargs['deadline'],180)

    def test_enabled_search_does_not_shorten_general_reply_deadline(self):
        self.cfg = update_config(self.cfg, 'search', enabled=True, total_timeout_seconds=15, owner=self)
        self.cfg = replace_config(self.cfg, 'models.reply.options.timeout_seconds', 240, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 200, owner=self)
        self.client.request.return_value=model_result(dict(content='普通回复'))
        with patch('wxbridge.ai.reply.runner.time.monotonic',return_value=100):
            self.run_reply()
        self.assertEqual(self.client.request.call_args.kwargs['deadline'],300)

    def test_external_search_consumes_own_timeout_and_other_tool_still_runs(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.cfg = update_config(self.cfg, 'search', enabled=True, backend='external', total_timeout_seconds=15, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 100, owner=self)
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True), user_notice='独立工具已完成'))
        clock = [100.0]
        def search(query, deadline):
            self.assertEqual((query, deadline), ('synthetic', 115))
            clock[0] = 115.0
            return dict(results=[dict(url='https://example.com/source', summary='synthetic')])
        self.backend.search.side_effect = search
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic')])),
            model_result(dict(tool_calls=[call('other', 'test_lookup')])),
            model_result(dict(content='完成')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.time.monotonic', side_effect=lambda: clock[0]), \
             patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        self.assertIn('核实不了', reply.text)
        self.assertIn('独立工具已完成', reply.text)
        handler.assert_called_once()
        offered = [tool['function']['name'] for tool in self.client.request.call_args_list[1].kwargs['tools']]
        self.assertNotIn('web_search', offered)
        self.assertNotIn('web_fetch', offered)
        self.assertIn('test_lookup', offered)
        self.assertEqual(self.client.request.call_args_list[1].kwargs['deadline'], 200)

    def test_search_failure_does_not_forward_model_claim_when_image_is_prepared(self):
        self.backend.search.side_effect = ToolError('search_timeout')
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic'),
                                          call('i', 'generate_image', prompt='cat')])),
            model_result(dict(content='我已核实结果确实如此。')),
        ]
        reply = self.run_reply()
        self.assertIn('联网查询没有完成', reply.text)
        self.assertNotIn('我已核实', reply.text)
        self.assertNotIn('在画', reply.text)
        self.assertEqual(reply.image_proposals, (ImageProposal(prompt='cat'),))

    def test_search_failure_preserves_only_explicit_trusted_notice_from_another_tool(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.backend.search.side_effect = ToolError('search_timeout')
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True), user_notice='独立工具的结果'))
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic'), call('other', 'test_lookup')])),
            model_result(dict(content='我已核实结果确实如此。')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        self.assertIn('联网查询没有完成', reply.text)
        self.assertIn('独立工具的结果', reply.text)
        self.assertNotIn('我已核实', reply.text)
        handler.assert_called_once()

    def test_search_failure_with_empty_model_reply_keeps_trusted_notice(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.backend.search.side_effect = ToolError('search_timeout')
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True), user_notice='独立工具的结果'))
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic'), call('other', 'test_lookup')])),
            model_result(dict(content='')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        self.assertIn('联网查询没有完成', reply.text)
        self.assertIn('独立工具的结果', reply.text)

    def test_search_failure_with_empty_model_reply_keeps_image_proposal(self):
        self.backend.search.side_effect = ToolError('search_timeout')
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic'),
                                          call('i', 'generate_image', prompt='cat')])),
            model_result(dict(content='')),
        ]
        reply = self.run_reply()
        self.assertIn('联网查询没有完成', reply.text)
        self.assertEqual(reply.image_proposals, (ImageProposal(prompt='cat'),))

    def test_search_failure_without_other_results_keeps_empty_reply_error(self):
        self.backend.search.side_effect = ToolError('search_timeout')
        for content in ('', '---'):
            with self.subTest(content=content):
                self.client.request.side_effect = [
                    model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic')])),
                    model_result(dict(content=content)),
                ]
                with self.assertRaisesRegex(ModelError, 'empty_reply'):
                    self.run_reply()

    def test_empty_model_reply_without_search_remains_an_error_even_with_notice(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.cfg = replace_config(self.cfg, 'search.enabled', False, owner=self)
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True), user_notice='独立工具的结果'))
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('other', 'test_lookup')])),
            model_result(dict(content='')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            with self.assertRaisesRegex(ModelError, 'empty_reply'):
                self.run_reply()

    def test_native_search_elapsed_budget_does_not_block_other_tools(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.cfg = update_config(self.cfg, 'search', enabled=True, backend='native', total_timeout_seconds=15, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 100, owner=self)
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True)))
        clock = [100.0]
        def first(*args, **kwargs):
            self.assertIsNotNone(kwargs['native_search'])
            clock[0] = 115.0
            return ModelResult(dict(tool_calls=[call('other', 'test_lookup')]), 'tool_calls', {},
                search=dict(actions=1, calls=1, sources=[dict(url='https://example.com/source')], citations=[], errors=[]))
        replies = iter([first, model_result(dict(content='完成'))])
        def request(*args, **kwargs):
            reply = next(replies)
            return reply(*args, **kwargs) if callable(reply) else reply
        self.client.request.side_effect = request
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.time.monotonic', side_effect=lambda: clock[0]), \
             patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        self.assertEqual(reply.text, '完成')
        handler.assert_called_once()
        self.assertIsNone(self.client.request.call_args_list[1].kwargs['native_search'])
        self.assertEqual(self.client.request.call_args_list[1].kwargs['deadline'], 200)

    def test_external_search_time_budget_excludes_model_time_between_calls(self):
        self.cfg = update_config(self.cfg, 'search', enabled=True, backend='external', total_timeout_seconds=15, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 100, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.max_rounds', 3, owner=self)
        clock = [100.0]
        def search(query, deadline):
            if query == 'first':
                self.assertEqual(deadline, 115)
                clock[0] = 106
            else:
                self.assertEqual((query, deadline), ('second', 145))
                clock[0] = 140
            return dict(results=[dict(url='https://example.com/source', summary='synthetic')])
        self.backend.search.side_effect = search
        replies = iter([model_result(dict(tool_calls=[call('s1', 'web_search', query='first')])),
                        model_result(dict(tool_calls=[call('s2', 'web_search', query='second')])),
                        model_result(dict(content='完成'))])
        def request(*args, **kwargs):
            reply = next(replies)
            if reply.message.get('tool_calls', [{}])[0].get('id') == 's2':
                clock[0] = 136  # Time spent by the reply model, not by search.
            return reply
        self.client.request.side_effect = request
        with patch('wxbridge.ai.reply.runner.time.monotonic', side_effect=lambda: clock[0]):
            reply = self.run_reply()
        self.assertEqual(reply.text, '完成')
        self.assertEqual(self.backend.search.call_count, 2)

    def test_external_search_deadline_respects_the_callers_earlier_deadline(self):
        self.cfg = update_config(self.cfg, 'search', enabled=True, backend='external', total_timeout_seconds=15, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.total_timeout_seconds', 100, owner=self)
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic')])),
                                           model_result(dict(content='完成'))]
        with patch('wxbridge.ai.reply.runner.time.monotonic', return_value=100):
            self.run_reply(deadline=108)
        self.backend.search.assert_called_once_with('synthetic', 108)

    def test_mixed_image_and_web_calls_all_return_results_in_original_order(self):
        calls = [call('image', 'generate_image', prompt='PRIVATE_DRAWING'),
                 call('search', 'web_search', query='synthetic'),
                 call('fetch', 'web_fetch', url='https://example.com/source')]
        self.client.request.side_effect = [model_result(dict(tool_calls=calls)), model_result(dict(content='核实后的回答'))]
        reply = self.run_reply()
        self.assertEqual(reply.image_proposals, (ImageProposal(prompt='PRIVATE_DRAWING'),))
        self.assertEqual(reply.search['calls'], 2)
        self.backend.search.assert_called_once()
        self.backend.fetch.assert_called_once()
        history = self.client.request.call_args.args[0]
        self.assertEqual([message['tool_call_id'] for message in history if message['role'] == 'tool'],
                         ['image', 'search', 'fetch'])
        self.assertEqual(json.loads(history[-3]['content'])['status'], 'prepared')
        self.assertNotIn('PRIVATE', repr(reply))

    def test_search_budget_does_not_disable_images(self):
        self.cfg = replace_config(self.cfg, 'search.max_requests', 1, owner=self)
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic')])),
            model_result(dict(tool_calls=[call('i', 'generate_image', prompt='cat')])), model_result(dict(content=''))]
        reply = self.run_reply()
        offered = self.client.request.call_args_list[1].kwargs['tools']
        self.assertEqual([tool['function']['name'] for tool in offered], ['generate_image'])
        self.assertEqual(reply.image_proposals, (ImageProposal(prompt='cat'),))
        self.assertEqual(reply.text, '')

    def test_search_budget_does_not_disable_or_block_an_unrelated_tool(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.cfg = replace_config(self.cfg, 'search.max_requests', 1, owner=self)
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True, value='synthetic result')))
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('s1', 'web_search', query='synthetic')])),
            model_result(dict(tool_calls=[call('other', 'test_lookup'),
                                          call('s2', 'web_search', query='exhausted')])),
            model_result(dict(content='完成')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        offered = [item['function']['name'] for item in self.client.request.call_args_list[1].kwargs['tools']]
        self.assertIn('test_lookup', offered)
        self.assertNotIn('web_search', offered)
        self.assertNotIn('web_fetch', offered)
        handler.assert_called_once()
        self.backend.search.assert_called_once()
        history = self.client.request.call_args.args[0]
        outputs = {message['tool_call_id']: json.loads(message['content']) for message in history
                   if message['role'] == 'tool'}
        self.assertEqual(outputs['other']['value'], 'synthetic result')
        self.assertFalse(outputs['s2']['ok'])
        self.assertEqual(reply.search['calls'], 1)

    def test_non_search_tool_rounds_use_general_limit_when_search_disabled(self):
        from wxbridge.ai.tools.registry import TOOLS
        self.cfg = replace_config(self.cfg, 'search.enabled', False, owner=self)
        self.cfg = replace_config(self.cfg, 'tools.max_rounds', 2, owner=self)
        definition = dict(type='function', function=dict(name='test_lookup', description='synthetic',
                          parameters=dict(type='object', properties={})))
        handler = Mock(return_value=ToolResult(dict(ok=True)))
        self.client.request.side_effect = [
            model_result(dict(tool_calls=[call('a', 'test_lookup')])),
            model_result(dict(tool_calls=[call('b', 'test_lookup')])),
            model_result(dict(content='完成')),
        ]
        def enabled(*args, **kwargs):
            return get_enabled_tools(*args, **kwargs) + [definition]
        with patch('wxbridge.ai.reply.runner.get_enabled_tools', side_effect=enabled), \
             patch.dict(TOOLS, {'test_lookup': (definition, handler)}):
            reply = self.run_reply()
        self.assertEqual(reply.text, '完成')
        self.assertEqual(handler.call_count, 2)
        self.assertIsNone(self.client.request.call_args_list[2].kwargs['tools'])

    def test_invalid_batch_has_no_partial_execution(self):
        for calls in ([call('same', 'web_search', query='synthetic'), call('same', 'generate_image', prompt='cat')],
                      [call('valid', 'web_search', query='synthetic'), dict(function=dict(name='generate_image', arguments='{}'))]):
            with self.subTest(calls=len(calls)):
                self.client.request.side_effect = [model_result(dict(tool_calls=calls))]
                with self.assertRaisesRegex(ModelError, 'invalid_tool_calls'):
                    self.run_reply()
        self.backend.search.assert_not_called()

    def test_repeated_call_id_is_rejected_before_a_second_execution(self):
        tool = call('same', 'web_search', query='synthetic')
        self.client.request.side_effect = [model_result(dict(tool_calls=[tool]))] * 2
        with self.assertRaisesRegex(ModelError, 'invalid_tool_calls'):
            self.run_reply()
        self.backend.search.assert_called_once()

    def test_disabled_unknown_and_bad_arguments_get_correlated_errors(self):
        self.cfg = replace_config(self.cfg, 'image_generation.enabled', False, owner=self)
        bad = call('bad-json', 'web_search')
        bad['function']['arguments'] = 'PRIVATE_INVALID_JSON'
        self.client.request.side_effect = [model_result(dict(tool_calls=[bad,
            call('unknown', 'not_a_tool'), call('disabled', 'generate_image', prompt='cat')])),
            model_result(dict(content='请换一种说法'))]
        reply = self.run_reply()
        outputs = [json.loads(message['content']) for message in self.client.request.call_args.args[0]
                   if message['role'] == 'tool']
        self.assertEqual([output['error'] for output in outputs], ['invalid_tool_arguments', 'invalid_tool', 'invalid_tool'])
        self.assertFalse(reply.image_proposals)
        self.assertNotIn('PRIVATE', json.dumps(reply.search))
        self.backend.search.assert_not_called()

    def test_only_one_image_is_prepared_and_second_call_receives_error(self):
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('a', 'generate_image', prompt='cat'),
            call('b', 'generate_image', prompt='dog')])), model_result(dict(content=''))]
        reply = self.run_reply()
        self.assertEqual(reply.image_proposals, (ImageProposal(prompt='cat'),))
        self.assertIn('image_tool_limit', reply.search['errors'])

    def test_deadline_and_cancellation_stop_between_tools(self):
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic'),
            call('i', 'generate_image', prompt='cat')]))]
        cancelled = False
        def check():
            if cancelled:
                raise CancelledError()
        def search(*args):
            nonlocal cancelled
            cancelled = True
            return dict(results=[])
        self.backend.search.side_effect = search
        with self.assertRaises(CancelledError):
            self.run_reply(check=check)
        self.assertEqual(self.client.request.call_count, 1)
        self.client.reset_mock()
        with self.assertRaisesRegex(ModelError, 'deadline_exceeded'):
            self.run_reply(deadline=time.monotonic() - 1)
        self.client.request.assert_not_called()

    def test_prepared_image_does_not_escape_after_final_response_is_cancelled(self):
        cancelled = False
        def check():
            if cancelled:
                raise CancelledError()
        def final(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return model_result(dict(content=''))
        first = model_result(dict(tool_calls=[call('i', 'generate_image', prompt='cat')]))
        responses = iter([first, None])
        self.client.request.side_effect = lambda *a, **kw: next(responses) or final()
        with self.assertRaises(CancelledError):
            self.run_reply(check=check)

    def test_round_limit_does_not_execute_extra_tools(self):
        self.cfg = replace_config(self.cfg, 'tools.max_rounds', 0, owner=self)
        self.client.request.return_value = model_result(dict(tool_calls=[call('s', 'web_search', query='synthetic')]))
        reply = self.run_reply()
        self.assertIn('tool_limit', reply.search['errors'])
        self.assertIsNone(self.client.request.call_args.kwargs['tools'])
        self.backend.search.assert_not_called()

    def test_corrupt_response_does_not_apply_an_earlier_image_proposal(self):
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('i', 'generate_image', prompt='cat')])),
                                           ModelError('invalid_response')]
        with self.assertRaisesRegex(ModelError, 'invalid_response'):
            self.run_reply()

    def test_catalog_and_config_are_isolated_across_requests(self):
        snapshot = deepcopy(self.cfg)
        definitions = get_enabled_tools(self.cfg, search_backend='external')
        definitions[0]['function']['name'] = 'changed'
        self.assertEqual(get_enabled_tools(self.cfg, search_backend='external')[0]['function']['name'], 'web_search')
        self.client.request.side_effect = [model_result(dict(tool_calls=[call('i', 'generate_image', prompt='cat')])),
            model_result(dict(content='')), model_result(dict(tool_calls=[call('i', 'generate_image', prompt='dog')])),
            model_result(dict(content=''))]
        self.assertEqual(self.run_reply().image_proposals, (ImageProposal(prompt='cat'),))
        self.assertEqual(self.run_reply().image_proposals, (ImageProposal(prompt='dog'),))
        self.assertEqual(self.cfg, snapshot)
        self.assertEqual(self.messages, [dict(role='user', content='PRIVATE_CHAT')])


class HostedToolCombinationTests(unittest.TestCase):
    def test_auto_respects_standalone_provider_capability_override(self):
        from dataclasses import replace
        from wxbridge.ai.tools.native_search import resolve_search_backend
        cfg=default_config()
        cfg=replace(cfg,search=replace(cfg.search,backend='auto'))
        client=Mock(spec=Client)
        client.adapter=Mock(native_search_supported=True)
        client.cfg={'native_search_supported':False}
        self.assertEqual(resolve_search_backend(cfg,client),'external')

    def test_hosted_search_and_custom_image_tool_replay_through_real_adapters(self):
        for protocol in ('openai_responses', 'anthropic'):
            with self.subTest(protocol=protocol):
                cfg = default_config()
                cfg = replace_config(cfg, 'search.backend', 'native')
                cfg = replace_config(cfg, 'image_generation.enabled', True)
                if protocol == 'openai_responses':
                    wire = dict(status='completed', output=[dict(type='function_call', call_id='i',
                        name='generate_image', arguments='{"prompt":"cat"}')])
                    final = dict(status='completed', output=[dict(type='message', content=[dict(type='output_text', text='')])])
                else:
                    wire = dict(content=[dict(type='thinking', thinking='', signature='PRIVATE_SIGNATURE'),
                        dict(type='tool_use', id='i', name='generate_image', input=dict(prompt='cat'))], stop_reason='tool_use')
                    final = dict(content=[dict(type='text', text='')], stop_reason='end_turn')
                with Client(dict(provider=protocol, model='synthetic', thinking='disabled',
                                 base_url='https://example.invalid/v1'), api_key='synthetic') as client:
                    with patch.object(client, '_post', side_effect=[wire, final]) as post, capture_metrics() as metrics:
                        reply = run_tool_loop(cfg, client, [dict(role='user', content='draw')])
                self.assertEqual(reply.image_proposals, (ImageProposal(prompt='cat'),))
                self.assertTrue(all(not entry['error'] for entry in metrics))
                tools = post.call_args_list[0].args[0]['tools']
                self.assertEqual(len(tools), 2)
                history = post.call_args_list[1].args[0]
                if protocol == 'openai_responses':
                    self.assertEqual(history['input'][-1]['call_id'], 'i')
                else:
                    self.assertEqual(history['messages'][-2]['content'][0]['signature'], 'PRIVATE_SIGNATURE')
                    self.assertEqual(history['messages'][-1]['content'][0]['tool_use_id'], 'i')

    def test_duplicate_names_native_name_collision_and_unknown_choice_are_rejected(self):
        for protocol in ('openai_responses', 'anthropic'):
            adapter = get_protocol_adapter(protocol)
            settings = dict(model='synthetic', thinking='disabled')
            collision = deepcopy(IMAGE_TOOL)
            collision['function']['name'] = 'web_search'
            for tools, native, code in (([IMAGE_TOOL, IMAGE_TOOL], None, 'invalid_tool_definition'),
                                         ([collision], {'max_calls': 1}, 'invalid_tool_definition')):
                with self.subTest(protocol=protocol, code=code), self.assertRaisesRegex(ModelError, code):
                    adapter.build_payload(settings, ModelRequest([], tools=tools, native_search=native))
            with self.assertRaisesRegex(ModelError, 'invalid_tool_choice'):
                adapter.build_payload(settings, ModelRequest([], tools=[IMAGE_TOOL],
                    tool_choice=dict(type='function', function=dict(name='unknown'))))


if __name__ == '__main__':
    unittest.main()
