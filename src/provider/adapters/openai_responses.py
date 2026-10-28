"""Stateless OpenAI Responses: multimodal input, tools and hosted web search."""
from copy import deepcopy

from .base import ModelError, ModelResult
from .openai_chat_completions import ChatCompletionsAdapter
from .common import (
    build_continuation_message, build_tool_call, extract_image_url_and_detail,
    extract_tool_definition, get_continuation_items, get_search_call_limit,
    guard_conversion_errors, nonnegative_int_or_none, parse_web_source,
)


class ResponsesAdapter(ChatCompletionsAdapter):
    native_search_supported = True

    @guard_conversion_errors('invalid_request')
    def build_payload(self, settings, request):
        inputs, pending, fulfilled = [], set(), set()
        for message in request.messages:
            role = message.get('role')
            raw = get_continuation_items(message, 'openai_responses')
            if raw is not None:
                if role != 'assistant':raise ModelError('invalid_message')
                inputs.extend(raw)
                pending.update(i['call_id'] for i in raw if i.get('type') == 'function_call')
                continue
            if role == 'tool':
                identifier = message.get('tool_call_id')
                if identifier not in pending or identifier in fulfilled:raise ModelError('invalid_tool_result')
                fulfilled.add(identifier)
                inputs.append(dict(type='function_call_output', call_id=identifier, output=message.get('content') or ''))
                continue
            if role not in ('system', 'developer', 'user', 'assistant'):raise ModelError('invalid_message')
            content = message.get('content')
            if isinstance(content, str):
                if content:inputs.append(dict(role=role, content=content))
            elif isinstance(content, list):
                blocks = []
                for block in content:
                    if block.get('type') == 'text':
                        blocks.append(dict(type='output_text' if role == 'assistant' else 'input_text', text=block['text']))
                    elif block.get('type') == 'image_url' and role == 'user':
                        url, detail = extract_image_url_and_detail(block)
                        blocks.append(dict(type='input_image', image_url=url, detail=detail))
                    else:raise ModelError('unsupported_content_block')
                inputs.append(dict(role=role, content=blocks))
            elif content is not None:raise ModelError('invalid_message')
            for call in message.get('tool_calls') or []:
                f = call['function'];identifier = call['id']
                if identifier in pending:raise ModelError('invalid_tool_result')
                pending.add(identifier)
                inputs.append(dict(type='function_call', call_id=identifier, name=f['name'], arguments=f['arguments']))
        payload = dict(model=settings['model'], input=inputs, max_output_tokens=settings.get('max_tokens', 1200),
                       stream=False, store=False, include=['reasoning.encrypted_content'])
        effort = 'none' if settings.get('thinking') == 'disabled' else settings.get('reasoning_effort')
        if effort is not None:payload['reasoning'] = dict(effort=effort)
        # Reasoning models reject sampling parameters when effort is not none.
        if effort == 'none' and settings.get('temperature') is not None:payload['temperature'] = settings['temperature']
        tools = []
        for tool in request.tools or []:
            f = extract_tool_definition(tool)
            tools.append(dict(type='function', name=f['name'], description=f.get('description', ''),
                              parameters=f['parameters'], strict=f.get('strict', False)))
        if request.native_search is not None:
            if tools:raise ModelError('mixed_search_tools')
            tools.append(dict(type='web_search'))
            payload['max_tool_calls'] = get_search_call_limit(request.native_search)
            payload['include'].append('web_search_call.action.sources')
        if tools:
            choice = request.tool_choice or 'auto'
            if isinstance(choice, dict):
                if choice.get('type') != 'function' or not isinstance(choice.get('function'), dict):raise ModelError('invalid_tool_choice')
                choice = dict(type='function', name=choice['function']['name'])
            elif choice not in ('auto', 'none', 'required'):raise ModelError('invalid_tool_choice')
            payload.update(tools=tools, tool_choice=choice)
        return payload

    @guard_conversion_errors('invalid_response')
    def parse_response(self, response):
        if not isinstance(response, dict) or not isinstance(response.get('output'), list):raise ModelError('invalid_response')
        status = response.get('status')
        if status not in ('completed', 'incomplete'):
            code = (response.get('error') or {}).get('code')
            raise ModelError('provider_failed', retryable=code in ('server_error', 'rate_limit_exceeded', 'timeout'))
        output = response['output'];texts, calls, citations, sources, errors = [], [], [], [], []
        searched = actions = text_offset = 0
        refused = False
        for item in output:
            if not isinstance(item, dict):raise ModelError('invalid_response')
            kind = item.get('type')
            if kind == 'message':
                for block in item.get('content', []):
                    if block.get('type') == 'refusal':refused = True
                    elif block.get('type') == 'output_text':
                        if not isinstance(block.get('text'), str):raise ModelError('invalid_response')
                        texts.append(block['text'])
                        for annotation in block.get('annotations', []):
                            if annotation.get('type') == 'url_citation':
                                citation = parse_web_source(annotation)
                                if citation:
                                    start, end = annotation.get('start_index'), annotation.get('end_index')
                                    if type(start) is int and type(end) is int and 0 <= start < end <= len(block['text']):
                                        citation.update(start_index=text_offset+start, end_index=text_offset+end)
                                    citations.append(citation)
                        text_offset += len(block['text'])+1
            elif kind == 'function_call':
                calls.append(build_tool_call(item.get('call_id'), item.get('name'), item.get('arguments')))
            elif kind == 'web_search_call':
                actions += 1
                action = item.get('action') or {}
                searched += int(action.get('type') == 'search')
                if item.get('status') != 'completed':errors.append('native_search_failed')
                for row in action.get('sources', []):
                    found = parse_web_source(row)
                    if found:sources.append(found)
            elif kind != 'reasoning':raise ModelError('unsupported_response_item')
        if len({c['id'] for c in calls}) != len(calls):raise ModelError('invalid_response')
        finish = 'tool_calls' if calls else 'stop'
        if status == 'incomplete':
            reason = (response.get('incomplete_details') or {}).get('reason')
            if reason == 'max_output_tokens':finish = 'length'
            elif reason == 'content_filter':finish = 'content_filter'
            else:raise ModelError('incomplete_response')
        if refused:finish = 'content_filter'
        usage = {};raw = response.get('usage') or {}
        for old, new in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')):
            if nonnegative_int_or_none(raw.get(old)) is not None:usage[new] = raw[old]
        for details, old, new in (('input_tokens_details', 'cached_tokens', 'cache_read_tokens'),
                                  ('output_tokens_details', 'reasoning_tokens', 'reasoning_tokens')):
            value = (raw.get(details) or {}).get(old)
            if nonnegative_int_or_none(value) is not None:usage[new] = value
        message = dict(content='\n'.join(texts), tool_calls=calls,
                       _provider_state=dict(provider='openai_responses', items=deepcopy(output)))
        return ModelResult(message, finish, usage, dict(attempted=actions > 0, calls=searched, actions=actions,
                           sources=sources, citations=citations, errors=errors))

    def assistant_message(self, message):
        return build_continuation_message(message, 'openai_responses')
