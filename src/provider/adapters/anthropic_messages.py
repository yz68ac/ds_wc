"""Anthropic Messages wire adapter, including signed thinking and server search."""
from copy import deepcopy
import json

from .base import ModelError, ModelResult
from .common import (
    build_anthropic_image_source, build_continuation_message, build_tool_call,
    extract_image_url_and_detail, extract_tool_definition, get_continuation_items,
    get_search_call_limit, guard_conversion_errors, nonnegative_int_or_none, parse_web_source,
)


class AnthropicAdapter:
    native_search_supported = True

    @guard_conversion_errors('invalid_request')
    def build_payload(self, settings, request):
        system, messages, pending, fulfilled = [], [], set(), set()
        def append(role, blocks):
            if not blocks:return
            if messages and messages[-1]['role'] == role:messages[-1]['content'].extend(blocks)
            else:messages.append(dict(role=role, content=blocks))
        for message in request.messages:
            role = message.get('role')
            raw = get_continuation_items(message, 'anthropic')
            if raw is not None:
                if role != 'assistant':raise ModelError('invalid_message')
                pending.update(b['id'] for b in raw if b.get('type') == 'tool_use')
                append('assistant', raw)
                continue
            if role in ('system', 'developer'):
                if not isinstance(message.get('content'), str):raise ModelError('invalid_message')
                system.append(message['content']);continue
            if role == 'tool':
                identifier = message.get('tool_call_id')
                if identifier not in pending or identifier in fulfilled:raise ModelError('invalid_tool_result')
                fulfilled.add(identifier)
                append('user', [dict(type='tool_result', tool_use_id=identifier, content=message.get('content') or '')])
                continue
            if role not in ('user', 'assistant'):raise ModelError('invalid_message')
            blocks = [];content = message.get('content')
            if isinstance(content, str):
                if content:blocks.append(dict(type='text', text=content))
            elif isinstance(content, list):
                for block in content:
                    if block.get('type') == 'text':blocks.append(dict(type='text', text=block['text']))
                    elif block.get('type') == 'image_url' and role == 'user':
                        url, _ = extract_image_url_and_detail(block);blocks.append(dict(type='image', source=build_anthropic_image_source(url)))
                    else:raise ModelError('unsupported_content_block')
            elif content is not None:raise ModelError('invalid_message')
            for call in message.get('tool_calls') or []:
                identifier = call['id'];f = call['function']
                if identifier in pending:raise ModelError('invalid_tool_result')
                pending.add(identifier)
                try:args = json.loads(f['arguments'])
                except (TypeError, ValueError):raise ModelError('invalid_tool_result') from None
                if not isinstance(args, dict):raise ModelError('invalid_tool_result')
                blocks.append(dict(type='tool_use', id=identifier, name=f['name'], input=args))
            append(role, blocks)
        payload = dict(model=settings['model'], messages=messages, max_tokens=settings.get('max_tokens', 1200), stream=False)
        if system:payload['system'] = '\n\n'.join(system)
        enabled = settings.get('thinking') != 'disabled'
        mode = settings.get('thinking_mode', 'adaptive')
        if enabled:
            if mode == 'manual':
                if settings.get('reasoning_effort') is not None:raise ModelError('unsupported_reasoning_effort')
                budget = settings.get('thinking_budget_tokens', 1024)
                if type(budget) is not int or not 1024 <= budget < payload['max_tokens']:raise ModelError('invalid_thinking_budget')
                payload['thinking'] = dict(type='enabled', budget_tokens=budget)
            elif mode == 'adaptive':
                payload['thinking'] = dict(type='adaptive')
                if settings.get('reasoning_effort') is not None:payload['output_config'] = dict(effort=settings['reasoning_effort'])
            else:raise ModelError('unsupported_thinking_mode')
        else:
            payload['thinking'] = dict(type='between_tools' if mode == 'between_tools' else 'disabled')
            # SDK 2.x uses the new API by default; sampling is legacy-only.
            if (settings.get('anthropic_version') and settings['anthropic_version'] < '2026-10-21'
                    and settings.get('temperature') is not None):
                payload['temperature'] = settings['temperature']
        tools = []
        for tool in request.tools or []:
            f = extract_tool_definition(tool)
            tools.append(dict(name=f['name'], description=f.get('description', ''), input_schema=f['parameters']))
        if request.native_search is not None:
            if tools:raise ModelError('mixed_search_tools')
            version = settings.get('anthropic_web_search_version', 'web_search_20250305')
            search = dict(type=version, name='web_search', max_uses=get_search_call_limit(request.native_search))
            if version != 'web_search_20250305':search['allowed_callers'] = ['direct']
            tools.append(search)
        if tools:
            choice = request.tool_choice or 'auto'
            if isinstance(choice, str):
                choice = {'auto': {'type': 'auto'}, 'none': {'type': 'none'}, 'required': {'type': 'any'}}.get(choice)
            elif isinstance(choice, dict) and choice.get('type') == 'function':
                choice = dict(type='tool', name=choice['function']['name'])
            else:choice = None
            if choice is None:raise ModelError('invalid_tool_choice')
            if enabled and mode == 'manual' and choice['type'] not in ('auto', 'none'):raise ModelError('unsupported_tool_choice')
            payload.update(tools=tools, tool_choice=choice)
        return payload

    @guard_conversion_errors('invalid_response')
    def parse_response(self, response):
        if not isinstance(response, dict) or not isinstance(response.get('content'), list):raise ModelError('invalid_response')
        reasons = {'end_turn': 'stop', 'stop_sequence': 'stop', 'max_tokens': 'length',
                   'model_context_window_exceeded': 'length', 'tool_use': 'tool_calls',
                   'pause_turn': 'pause_turn', 'refusal': 'content_filter'}
        finish = reasons.get(response.get('stop_reason'))
        if finish is None:raise ModelError('invalid_response')
        if (response.get('stop_details') or {}).get('type') == 'refusal':finish = 'content_filter'
        texts, calls, citations, sources, errors = [], [], [], [], []
        count = blocked = 0
        for block in response['content']:
            if not isinstance(block, dict):raise ModelError('invalid_response')
            kind = block.get('type')
            if kind == 'text':
                if not isinstance(block.get('text'), str):raise ModelError('invalid_response')
                texts.append(block['text'])
                for citation in block.get('citations', []):
                    if citation.get('type') == 'web_search_result_location':
                        found = parse_web_source(citation)
                        if found:citations.append(found)
            elif kind == 'tool_use':calls.append(build_tool_call(block.get('id'), block.get('name'), block.get('input')))
            elif kind == 'server_tool_use':
                if block.get('name') != 'web_search':raise ModelError('unsupported_response_item')
                count += 1
            elif kind == 'web_search_tool_result':
                rows = block.get('content')
                if isinstance(rows, dict):
                    errors.append('native_search_failed')
                    blocked += int(rows.get('error_code') == 'max_uses_exceeded')
                elif isinstance(rows, list):
                    for row in rows:
                        found = parse_web_source(row)
                        if found:sources.append(found)
                else:raise ModelError('invalid_response')
            elif kind not in ('thinking', 'redacted_thinking'):raise ModelError('unsupported_response_item')
        if len({c['id'] for c in calls}) != len(calls):raise ModelError('invalid_response')
        if finish == 'tool_calls' and not calls:raise ModelError('invalid_response')
        raw = response.get('usage') or {};usage = {}
        prompt = nonnegative_int_or_none(raw.get('input_tokens'))
        for old, new in (('cache_read_input_tokens', 'cache_read_tokens'), ('cache_creation_input_tokens', 'cache_write_tokens')):
            value = nonnegative_int_or_none(raw.get(old))
            if value is not None:
                usage[new] = value
                if prompt is not None:prompt += value
        if prompt is not None:usage['prompt_tokens'] = prompt
        if nonnegative_int_or_none(raw.get('output_tokens')) is not None:usage['completion_tokens'] = raw['output_tokens']
        if all(k in usage for k in ('prompt_tokens', 'completion_tokens')):
            usage['total_tokens'] = usage['prompt_tokens']+usage['completion_tokens']
        thinking = nonnegative_int_or_none((raw.get('output_tokens_details') or {}).get('thinking_tokens'))
        if thinking is not None:usage['reasoning_tokens'] = thinking
        message = dict(content='\n'.join(texts), tool_calls=calls,
                       _provider_state=dict(provider='anthropic', items=deepcopy(response['content'])))
        return ModelResult(message, finish, usage, dict(attempted=count > 0, calls=max(0, count-blocked), actions=max(0, count-blocked),
                           sources=sources, citations=citations, errors=errors))

    def assistant_message(self, message):
        return build_continuation_message(message, 'anthropic')
