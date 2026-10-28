"""Explicit adapters; adding a wire protocol does not change business callers."""
from types import MappingProxyType

from .base import ModelError, ModelRequest, ModelResult, ProtocolAdapter
from .openai_chat_completions import ChatCompletionsAdapter
from .deepseek import DeepSeekAdapter
from .openai_responses import ResponsesAdapter
from .anthropic_messages import AnthropicAdapter

_ADAPTERS = MappingProxyType({
    'deepseek': DeepSeekAdapter,
    'openai': ChatCompletionsAdapter,  # Existing configuration spelling.
    'openai_compatible': ChatCompletionsAdapter,
    'openai_responses': ResponsesAdapter,
    'anthropic': AnthropicAdapter,
    'anthropic_messages': AnthropicAdapter,
})


def supported_protocols():
    return tuple(_ADAPTERS)


def get_protocol_adapter(protocol):
    if not isinstance(protocol, str) or protocol not in _ADAPTERS:
        raise ModelError('unsupported_provider')
    return _ADAPTERS[protocol]()


# Compatibility names for existing integrations; new code uses protocol names.
ProviderAdapter = ProtocolAdapter
get_adapter = get_protocol_adapter
supported_providers = supported_protocols

__all__ = ['ModelError', 'ModelRequest', 'ModelResult', 'ProtocolAdapter',
           'get_protocol_adapter', 'supported_protocols',
           'ProviderAdapter', 'get_adapter', 'supported_providers']
