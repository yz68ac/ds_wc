"""One bounded reply/tool loop; returns proposals without changing owner state."""
from copy import deepcopy
import json
import time

from provider import ModelError
from .citations import without_citations
from ..tools.native_search import native_search_options, record_native_search, resolve_search_backend
from .output import format_reply, response_prompt
from ..tools.registry import apply_tool_instructions, dispatch_tool_call, get_enabled_tools, validate_tool_calls
from ..tools.base import ToolContext, ToolReply


def run_tool_loop(cfg, client, messages, on_search=None, *, search_client=None, deadline=None, check=None):
    settings = deepcopy(cfg)
    search = settings.get('search', {})
    end = time.monotonic() + float(search.get('total_timeout_seconds', 120))
    if deadline is not None:
        end = min(end, deadline)
    backend = resolve_search_backend(settings, client)
    definitions = get_enabled_tools(settings, search_backend=backend)
    context = ToolContext(settings=settings, backend=backend, deadline=end,
        allowed_tools=frozenset(tool['function']['name'] for tool in definitions),
        search_client=search_client, on_search=on_search, check=check)
    conversation = apply_tool_instructions(settings, response_prompt(messages, settings), tools=definitions)

    def finish(text, annotations=()):
        context.check_active()
        text = format_reply(without_citations(text, context.sources, annotations), settings).strip()
        if not text and not context.image_requests:
            raise ModelError('empty_reply', retryable=True)
        return ToolReply(text, context.metadata(), tuple(deepcopy(context.image_requests)))

    maximum_rounds = int(search.get('max_tool_rounds', 2))
    for turn in range(maximum_rounds + 1):
        context.check_active()
        exhausted = turn >= maximum_rounds
        tools = [] if exhausted else [tool for tool in definitions
            if ((tool['function']['name'] == 'generate_image' and not context.image_requests)
                or (tool['function']['name'] != 'generate_image' and context.remaining_search_calls > 0))]
        native = native_search_options(context, exhausted=exhausted)
        try:
            result = client.request(conversation, tools=tools or None, native_search=native,
                                    tool_choice='auto' if tools or native else 'none', deadline=end)
            context.check_active()
            annotations = (record_native_search(context, result, native['max_calls'] if native else 0)
                           if backend == 'native' else [])
            if result.finish_reason == 'pause_turn':
                if result.message.get('tool_calls'):
                    raise ModelError('invalid_tool_calls')
                if exhausted or context.remaining_search_calls == 0:
                    context.errors.append('tool_limit')
                    return finish('这次查询到上限了，暂时还没能整理完整')
                conversation.append(client.assistant_message(result.message))
                continue
            prepared_only = (context.image_requests and result.finish_reason == 'stop'
                             and not result.message.get('content') and not result.message.get('tool_calls'))
            if not prepared_only:
                client.check_complete(result)
        except ModelError as exc:
            context.check_active()
            if exc.code in ('invalid_response', 'invalid_request', 'invalid_native_search_result',
                            'invalid_tool_calls', 'invalid_tool_result', 'continuation_missing',
                            'continuation_provider_mismatch'):
                raise
            if exc.code == 'empty_reply':
                if context.image_requests:
                    return finish('')
                raise
            if not context.attempted and not context.image_requests:
                raise
            context.errors.append('answer_generation_failed')
            return finish('刚才查到了一些资料，但整理回答超时了，稍后再试。' if context.sources
                          else '这次联网没能完成，我暂时核实不了，稍后再试。' if context.attempted else '')

        calls = result.message.get('tool_calls')
        if calls is None:
            calls = []
        validate_tool_calls(calls, context)
        if not calls:
            text = result.message.get('content') or ''
            if not format_reply(text, settings).strip() and not context.image_requests:
                return finish('')
            if context.attempted and ('native_search_failed' in context.errors or not context.sources):
                text = ('这次联网查询没有完成，暂时核实不了，稍后再试。' if context.errors
                        else '这次没有查到可用的网页结果，暂时核实不了。你可以换个更具体的关键词，我再查。')
            return finish(text, annotations)
        if exhausted:
            context.errors.append('tool_limit')
            return finish('这次工具调用已到上限，暂时还没能整理完整。')
        conversation.append(client.assistant_message(result.message))
        for call in calls:
            output = dispatch_tool_call(call, context)
            conversation.append(dict(role='tool', tool_call_id=call['id'],
                                     content=json.dumps(output.output, ensure_ascii=False)))

    context.errors.append('tool_limit')
    return finish('这次工具调用已到上限，暂时还没能整理完整。')
