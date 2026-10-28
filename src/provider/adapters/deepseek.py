"""DeepSeek parameter dialect over the shared Chat Completions transport."""
from .openai_chat_completions import ChatCompletionsAdapter


class DeepSeekAdapter(ChatCompletionsAdapter):
    def build_payload(self, settings, request):
        payload = super().build_payload(settings, request)
        if settings.get('thinking') in ('enabled', 'disabled'):
            payload['thinking'] = {'type': settings['thinking']}
        return payload
