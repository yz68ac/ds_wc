"""Normalize legacy configuration without modifying the caller's files."""
from copy import deepcopy
from .models import ConfigError


def normalize_tool_rounds(document):
    """Read the old search-scoped tool limit without changing the input."""
    search = document.get('search')
    if not isinstance(search, dict) or 'max_tool_rounds' not in search:
        return document
    value = deepcopy(document)
    legacy = value['search'].pop('max_tool_rounds')
    tools = value.setdefault('tools', {})
    if isinstance(tools, dict):
        tools.setdefault('max_rounds', legacy)
    return value


def normalize_transport_config(document):
    if 'native_api' not in document:return deepcopy(document)
    if 'helper_transport' in document or 'submission_api' in document:
        raise ConfigError('mixed_native_configuration')
    value = deepcopy(document)
    legacy = value.pop('native_api')
    fields = {'enabled','socket','helper_socket','allowed_chat_ids','max_text_bytes','max_pending','max_requests'}
    if not isinstance(legacy, dict) or set(legacy)-fields:
        raise ConfigError('invalid_native_config')
    value['helper_transport'] = {key: legacy[key] for key in ('enabled','allowed_chat_ids','max_text_bytes') if key in legacy}
    if 'helper_socket' in legacy:value['helper_transport']['socket'] = legacy['helper_socket']
    value['submission_api'] = {key: legacy[key] for key in ('enabled','socket','max_pending','max_requests') if key in legacy}
    return value
