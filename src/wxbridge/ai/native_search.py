"""Bounded hosted-search orchestration; no external search fallback or UI I/O."""
import time

from .client import ModelError
from .output import format_reply
from .citations import without_citations
from provider.adapters.common import is_public_url_candidate


def answer_with_native_search(cfg, client, messages, on_search=None, *, image_requests=None):
    from .search_prompt import search_prompt
    settings = cfg.get('search', {})
    deadline = time.monotonic()+float(settings.get('total_timeout_seconds', 120))
    maximum = int(settings.get('max_requests', 3))
    conversation = search_prompt(cfg, messages, native=True)
    image_enabled = cfg.get('image_generation', {}).get('enabled', False) and image_requests is not None
    if image_enabled:
        from .image_generation import IMAGE_TOOL, collect_image_request
    sources, errors = [], []
    attempted = notified = False
    calls = actions = 0

    def finish(text, annotations=()):
        text = format_reply(without_citations(text, sources, annotations), cfg)
        if not text.strip():raise ModelError('empty_reply', retryable=True)
        return text, dict(attempted=attempted, calls=calls, results=len(sources),
                          errors=list(dict.fromkeys(errors)), backend='native', actions=actions)

    for _ in range(int(settings.get('max_tool_rounds', 2))+1):
        remaining = maximum-actions
        if remaining <= 0:
            errors.append('tool_limit');return finish('这次查询到上限了，暂时还没能整理完整')
        if time.monotonic() >= deadline:raise ModelError('deadline_exceeded', retryable=True)
        result = client.request(conversation, deadline=deadline, native_search={'max_calls': remaining},
                                **(dict(tools=[IMAGE_TOOL]) if image_enabled else {}))
        info = result.search
        if not isinstance(info, dict):raise ModelError('invalid_native_search_result')
        used, searched = info.get('actions', 0), info.get('calls', 0)
        if type(used) is not int or type(searched) is not int or not 0 <= searched <= used <= remaining:
            raise ModelError('invalid_native_search_result')
        actions += used;calls += searched;attempted |= bool(info.get('attempted'))
        errors.extend('native_search_failed' for _ in info.get('errors', []))
        for row in [*info.get('citations', []), *info.get('sources', [])]:
            if isinstance(row, dict) and is_public_url_candidate(row.get('url')) and not any(s['url'] == row['url'] for s in sources):
                sources.append({'url': row['url']})
        if attempted and on_search and not notified:
            on_search();notified = True
        if result.finish_reason == 'pause_turn':
            conversation.append(client.assistant_message(result.message))
            continue  # Signed/opaque state is replayed unchanged, within budget.
        client.check_complete(result)
        if image_enabled and collect_image_request(result.message.get('tool_calls') or [], image_requests):
            return finish('在画了，稍后发给你')
        if result.message.get('tool_calls'):raise ModelError('unexpected_client_tool')
        if errors:return finish('这次联网查询没有完成，暂时核实不了')
        if attempted and not sources:return finish('这次没有查到可核对的来源，暂时核实不了')
        return finish(result.message.get('content') or '', info.get('citations', []))
    errors.append('tool_limit')
    return finish('这次查询还没完成，先到这里')
