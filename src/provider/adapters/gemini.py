"""Gemini generateContent adapter; signed native parts stay inside the provider."""
import base64
from copy import deepcopy
import json
import re
import uuid

from .base import ModelError, ModelResult
from .common import (build_continuation_message, build_tool_call, extract_image_url_and_detail,
                     extract_tool_definition, get_continuation_items, guard_conversion_errors,
                     nonnegative_int_or_none)


def _field(value, snake, camel):
    return value.get(snake, value.get(camel))


def _call(part):
    return _field(part, 'function_call', 'functionCall')


def _arguments(call):
    if not isinstance(call, dict) or call.get('type') != 'function':
        raise ModelError('invalid_tool_result')
    function = call.get('function')
    if not isinstance(function, dict):raise ModelError('invalid_tool_result')
    args = json.loads(function['arguments'])
    if not isinstance(args, dict):raise ModelError('invalid_tool_result')
    json.dumps(args, allow_nan=False)
    return function['name'], args


class GeminiAdapter:
    native_search_supported = False

    @guard_conversion_errors('invalid_request')
    def build_payload(self, settings, request):
        if request.native_search is not None:raise ModelError('unsupported_native_search')
        system, contents, pending, results, used = [], [], {}, {}, set()

        def append(role, parts):
            if not parts:return
            if contents and contents[-1]['role'] == role:contents[-1]['parts'].extend(parts)
            else:contents.append(dict(role=role, parts=parts))

        def register(parts, calls):
            native = [p for p in parts if _call(p) is not None]
            if not isinstance(calls, list) or len(native) != len(calls):
                raise ModelError('invalid_tool_result')
            for part, call in zip(native, calls):
                name, args = _arguments(call)
                raw = _call(part)
                identifier = call.get('id')
                if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256 or identifier in used
                        or not isinstance(raw, dict) or raw.get('name') != name or raw.get('args', {}) != args
                        or raw.get('id') not in (None, identifier)):
                    raise ModelError('invalid_tool_result')
                used.add(identifier)
                pending[identifier] = dict(name=name, native_id=raw.get('id'))

        for message in request.messages:
            if not isinstance(message, dict):raise ModelError('invalid_message')
            role = message.get('role')
            if role in ('system', 'developer'):
                if not isinstance(message.get('content'), str):raise ModelError('invalid_message')
                system.append(message['content']);continue
            if role == 'tool':
                identifier = message.get('tool_call_id')
                if not isinstance(identifier, str) or identifier not in pending or identifier in results:
                    raise ModelError('invalid_tool_result')
                value = message.get('content')
                if not isinstance(value, str):raise ModelError('invalid_tool_result')
                try:output = json.loads(value)
                except ValueError:output = {'result': value}
                if not isinstance(output, dict):output = {'result': output}
                json.dumps(output, allow_nan=False)
                source = pending[identifier]
                response = dict(name=source['name'], response=output)
                if source['native_id'] is not None:response['id'] = source['native_id']
                results[identifier] = dict(function_response=response)
                if len(results) == len(pending):
                    append('user', [results[key] for key in pending])
                    pending.clear();results.clear()
                continue
            if pending:raise ModelError('invalid_tool_result')
            if role not in ('user', 'assistant'):raise ModelError('invalid_message')
            raw = get_continuation_items(message, 'gemini')
            if raw is not None:
                if role != 'assistant' or any(not isinstance(p, dict) for p in raw):
                    raise ModelError('invalid_message')
                register(raw, message.get('tool_calls', []))
                append('model', raw);continue
            content = message.get('content')
            parts = []
            if isinstance(content, str):
                if content:parts.append(dict(text=content))
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):raise ModelError('invalid_message')
                    if block.get('type') == 'text' and isinstance(block.get('text'), str):
                        parts.append(dict(text=block['text']))
                    elif block.get('type') == 'image_url' and role == 'user':
                        url, _ = extract_image_url_and_detail(block)
                        match = re.fullmatch(r'data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\r\n]+)', url)
                        if not match:raise ModelError('invalid_image_input')
                        encoded = re.sub(r'\s', '', match[2])
                        base64.b64decode(encoded, validate=True)
                        parts.append(dict(inline_data=dict(mime_type=match[1], data=encoded)))
                    else:raise ModelError('unsupported_content_block')
            elif content is not None:raise ModelError('invalid_message')
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list) or calls and role != 'assistant':raise ModelError('invalid_tool_result')
            for call in calls:
                name, args = _arguments(call)
                parts.append(dict(function_call=dict(id=call['id'], name=name, args=args)))
            register(parts, calls)
            append('model' if role == 'assistant' else 'user', parts)
        if pending:raise ModelError('invalid_tool_result')
        config = dict(automatic_function_calling=dict(disable=True))
        if system:config['system_instruction'] = dict(parts=[dict(text='\n\n'.join(system))])
        if settings.get('max_tokens', 1200) is not None:config['max_output_tokens'] = settings.get('max_tokens', 1200)
        if settings.get('temperature') is not None:config['temperature'] = settings['temperature']
        thinking = settings.get('thinking', 'disabled')
        effort = settings.get('reasoning_effort')
        mode = settings.get('thinking_mode', 'adaptive')
        if mode not in ('adaptive', 'manual'):raise ModelError('unsupported_thinking_mode')
        if thinking == 'disabled' or effort == 'none':
            config['thinking_config'] = dict(thinking_budget=0)
        elif mode == 'manual' or settings.get('thinking_budget_tokens') is not None:
            budget = settings.get('thinking_budget_tokens')
            if type(budget) is not int or not 1 <= budget <= 128000:raise ModelError('invalid_thinking_budget')
            if effort is not None:raise ModelError('unsupported_reasoning_effort')
            config['thinking_config'] = dict(thinking_budget=budget)
        elif effort is not None:
            if effort not in ('low', 'medium', 'high'):raise ModelError('unsupported_reasoning_effort')
            config['thinking_config'] = dict(thinking_level=effort.upper())
        declarations, names = [], set()
        for tool in request.tools or []:
            function = extract_tool_definition(tool)
            if function['name'] in names:raise ModelError('invalid_tool_definition')
            names.add(function['name'])
            declarations.append(dict(name=function['name'], description=function.get('description', ''),
                                     parameters_json_schema=function['parameters']))
        if declarations:
            config['tools'] = [dict(function_declarations=declarations)]
            choice = request.tool_choice or 'auto'
            if isinstance(choice, str):
                mode = {'auto':'AUTO', 'none':'NONE', 'required':'ANY'}.get(choice)
                if mode is None:raise ModelError('invalid_tool_choice')
                calling = dict(mode=mode)
            elif isinstance(choice, dict) and choice.get('type') == 'function':
                name = choice['function']['name']
                if name not in names:raise ModelError('invalid_tool_choice')
                calling = dict(mode='ANY', allowed_function_names=[name])
            else:raise ModelError('invalid_tool_choice')
            config['tool_config'] = dict(function_calling_config=calling)
        elif request.tool_choice not in (None, 'auto', 'none'):
            raise ModelError('invalid_tool_choice')
        return dict(model=settings['model'], contents=contents, config=config)

    @guard_conversion_errors('invalid_response')
    def parse_response(self, response):
        if not isinstance(response, dict):raise ModelError('invalid_response')
        candidates = response.get('candidates') or []
        feedback = _field(response, 'prompt_feedback', 'promptFeedback') or {}
        if not isinstance(candidates, list) or not isinstance(feedback, dict):raise ModelError('invalid_response')
        blocked = _field(feedback, 'block_reason', 'blockReason')
        candidate = candidates[0] if candidates else {}
        if not isinstance(candidate, dict):raise ModelError('invalid_response')
        reason = _field(candidate, 'finish_reason', 'finishReason') or blocked
        if not isinstance(reason, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}', reason):
            raise ModelError('invalid_response')
        content = candidate.get('content') or {}
        if not isinstance(content, dict) or content.get('role') not in (None, 'model'):raise ModelError('invalid_response')
        parts = content.get('parts') or []
        if not isinstance(parts, list):raise ModelError('invalid_response')
        texts, calls, identifiers = [], [], set()
        for part in parts:
            if not isinstance(part, dict):raise ModelError('invalid_response')
            if part.get('thought') is not None and type(part['thought']) is not bool:raise ModelError('invalid_response')
            function = _call(part)
            if function is not None:
                if not isinstance(function, dict) or part.get('thought') is True or 'text' in part:raise ModelError('invalid_response')
                identifier = function.get('id')
                if identifier is None:identifier = 'gemini_call_'+uuid.uuid4().hex
                args = function.get('args', {})
                json.dumps(args, allow_nan=False)
                call = build_tool_call(identifier, function.get('name'), args)
                if identifier in identifiers:raise ModelError('invalid_response')
                identifiers.add(identifier);calls.append(call)
            elif 'text' in part:
                if not isinstance(part['text'], str):raise ModelError('invalid_response')
                if part.get('thought') is not True:texts.append(part['text'])
            elif not _field(part, 'thought_signature', 'thoughtSignature'):
                raise ModelError('unsupported_response_item')
        if blocked and blocked != 'BLOCK_REASON_UNSPECIFIED':finish = 'content_filter'
        elif reason == 'MAX_TOKENS':finish = 'length'
        elif reason in ('SAFETY','RECITATION','BLOCKLIST','PROHIBITED_CONTENT','SPII',
                        'IMAGE_SAFETY','IMAGE_PROHIBITED_CONTENT','IMAGE_RECITATION','MODEL_ARMOR'):
            finish = 'content_filter'
        elif reason in ('MALFORMED_FUNCTION_CALL','UNEXPECTED_TOOL_CALL'):finish = 'invalid_tool_calls'
        elif reason == 'STOP':finish = 'tool_calls' if calls else 'stop'
        else:finish = 'incomplete_response'
        raw = _field(response, 'usage_metadata', 'usageMetadata') or {}
        if not isinstance(raw, dict):raise ModelError('invalid_response')
        usage = {}
        for snake, camel, name in (
            ('prompt_token_count','promptTokenCount','prompt_tokens'),
            ('candidates_token_count','candidatesTokenCount','completion_tokens'),
            ('total_token_count','totalTokenCount','total_tokens'),
            ('thoughts_token_count','thoughtsTokenCount','reasoning_tokens'),
            ('cached_content_token_count','cachedContentTokenCount','cache_read_tokens')):
            value = nonnegative_int_or_none(_field(raw, snake, camel))
            if value is not None:usage[name] = value
        if 'completion_tokens' in usage:usage['completion_tokens'] += usage.get('reasoning_tokens', 0)
        message = dict(content=''.join(texts), tool_calls=calls,
                       _provider_state=dict(provider='gemini', items=deepcopy(parts)))
        return ModelResult(message, finish, usage, native_finish_reason=reason)

    def assistant_message(self, message):
        return build_continuation_message(message, 'gemini')
