"""Explicit adapters; adding a wire protocol does not change business callers."""
from types import MappingProxyType

from .base import ModelError, ModelRequest, ModelResult, ProviderAdapter
from .chat_completions import ChatCompletionsAdapter
from .deepseek import DeepSeekAdapter
from .responses import ResponsesAdapter
from .anthropic import AnthropicAdapter

_ADAPTERS = MappingProxyType({
    'deepseek': DeepSeekAdapter,
    'openai': ChatCompletionsAdapter,  # Existing configuration spelling.
    'openai_compatible': ChatCompletionsAdapter,
    'openai_responses': ResponsesAdapter,
    'anthropic': AnthropicAdapter,
    'anthropic_messages': AnthropicAdapter,
})


def supported_providers():
    return tuple(_ADAPTERS)


def get_adapter(provider):
    if not isinstance(provider, str) or provider not in _ADAPTERS:
        raise ModelError('unsupported_provider')
    return _ADAPTERS[provider]()


__all__ = ['ModelError', 'ModelRequest', 'ModelResult', 'ProviderAdapter',
           'get_adapter', 'supported_providers']
