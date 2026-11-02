"""Hosted-search selection and accounting; reply orchestration owns the call loop."""
from provider import Client, ModelError
from provider.adapters.common import is_public_url_candidate


def resolve_search_backend(settings, client):
    if not settings.search.enabled:
        return 'external'
    kind = settings.search.backend
    if kind == 'auto':
        supported = getattr(client.adapter, 'native_search_supported', False)
        # Standalone provider clients own SDK settings dictionaries; the bot's
        # RoleRequest uses its immutable resolved configuration instead.
        if isinstance(client, Client):
            supported = supported and client.cfg.get('native_search_supported', True)
        kind = 'native' if supported else 'external'
    if kind not in ('external', 'native'):
        raise ModelError('unsupported_search_backend')
    return kind


def native_search_options(context, *, exhausted):
    if (context.backend != 'native' or exhausted
            or not context.settings.search.enabled
            or context.remaining_search_calls == 0):
        return None
    return dict(max_calls=min(5, context.remaining_search_calls))


def record_native_search(context, result, limit):
    info = result.search
    if not isinstance(info, dict):
        raise ModelError('invalid_native_search_result')
    used, searched = info.get('actions', 0), info.get('calls', 0)
    if type(used) is not int or type(searched) is not int or not 0 <= searched <= used <= limit:
        raise ModelError('invalid_native_search_result')
    context.actions += used
    context.calls += searched
    errors = info.get('errors', [])
    citations, sources = info.get('citations', []), info.get('sources', [])
    if not all(isinstance(items, list) for items in (errors, citations, sources)):
        raise ModelError('invalid_native_search_result')
    context.errors.extend('native_search_failed' for _ in errors)
    for row in [*citations, *sources]:
        if (isinstance(row, dict) and is_public_url_candidate(row.get('url'))
                and not any(source['url'] == row['url'] for source in context.sources)):
            context.sources.append(dict(url=row['url']))
    if used or info.get('attempted'):
        context.search_started()
    return citations
