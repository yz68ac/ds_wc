"""DeepSeek parameter dialect over the shared Chat Completions transport."""
from .chat_completions import ChatCompletionsAdapter


class DeepSeekAdapter(ChatCompletionsAdapter):
    def encode(self, settings, request):
        payload = super().encode(settings, request)
        if settings.get('thinking') in ('enabled', 'disabled'):
            payload['thinking'] = {'type': settings['thinking']}
        return payload
