"""Bounded protocol conversions; no network, configuration loading or logging."""
from copy import deepcopy
from functools import wraps
import json
import ipaddress
import re
from urllib.parse import urlsplit

from .base import ModelError


def conversion(error_code):
    """Malformed wire data must not expose provider bodies via Python errors."""
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:return function(*args, **kwargs)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError):
                raise ModelError(error_code) from None
        return guarded
    return decorate


def integer(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def image_source(block):
    value = block.get('image_url') or {}
    url = value.get('url') if isinstance(value, dict) else None
    if not isinstance(url, str):
        raise ModelError('invalid_image_input')
    return url, value.get('detail', 'auto')


def tool_definition(tool):
    f = tool.get('function') if isinstance(tool, dict) and tool.get('type') == 'function' else None
    if not isinstance(f, dict) or not isinstance(f.get('name'), str) or not f['name'] or not isinstance(f.get('parameters'), dict):
        raise ModelError('invalid_tool_definition')
    return deepcopy(f)


def tool_call(identifier, name, arguments):
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256 or not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ModelError('invalid_response')
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):raise ValueError()
        encoded = json.dumps(args, ensure_ascii=False)
    except (ValueError, TypeError):
        raise ModelError('invalid_response') from None
    return dict(id=identifier, type='function', function=dict(name=name, arguments=encoded))


def state_items(message, provider):
    state = message.get('_provider_state')
    if state is None:return None
    if not isinstance(state, dict) or state.get('provider') != provider or not isinstance(state.get('items'), list):
        raise ModelError('continuation_provider_mismatch')
    return deepcopy(state['items'])


def assistant_state(message, provider):
    items = state_items(message, provider)
    if items is None:raise ModelError('continuation_missing')
    return dict(role='assistant', content=message.get('content'),
                tool_calls=deepcopy(message.get('tool_calls', [])),
                _provider_state=dict(provider=provider, items=items))


def search_limit(options):
    if (not isinstance(options, dict) or set(options) != {'max_calls'}
            or type(options['max_calls']) is not int or not 1 <= options['max_calls'] <= 5):
        raise ModelError('invalid_native_search_options')
    return options['max_calls']


def source(value):
    if not isinstance(value, dict) or not public_url(value.get('url')):
        return None
    return dict(url=value['url'], title=str(value.get('title') or '')[:200])


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):return False
    try:
        parsed = urlsplit(value);host = parsed.hostname or ''
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or not host or host.endswith(('.local', '.internal', '.localhost')):return False
        try:return ipaddress.ip_address(host).is_global
        except ValueError:return '.' in host and host != 'localhost'
    except ValueError:return False


def cited_text(text, annotations):
    """Plain clickable URLs at citation locations, without Markdown or raw IDs."""
    inserts, seen = {}, set()
    for annotation in annotations:
        found = source(annotation)
        if found and found['url'] not in seen and found['url'] not in text:
            end = annotation.get('end_index', len(text))
            if type(end) is not int or not 0 <= end <= len(text):end = len(text)
            inserts.setdefault(end, []).append(found['url']);seen.add(found['url'])
    for end in sorted(inserts, reverse=True):
        text = text[:end]+'（'+'；'.join(inserts[end])+'）'+text[end:]
    return text


def data_image(url):
    match = re.fullmatch(r'data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\r\n]+)', url)
    if match:return dict(type='base64', media_type=match[1], data=match[2])
    if url.startswith('https://'):return dict(type='url', url=url)
    raise ModelError('invalid_image_input')
