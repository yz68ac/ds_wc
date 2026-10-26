"""Current internal message shape to Chat Completions wire format."""
from __future__ import annotations

from copy import deepcopy

from .base import ModelError, ModelRequest, ModelResult


class ChatCompletionsAdapter:
    def endpoint(self, base_url):
        return base_url.rstrip('/')+'/chat/completions'

    def headers(self, api_key):
        return {'Content-Type': 'application/json', 'Authorization': 'Bearer '+api_key}

    def encode(self, settings, request: ModelRequest):
        payload = dict(model=settings['model'], messages=deepcopy(request.messages),
                       temperature=settings.get('temperature', 1.0),
                       max_tokens=settings.get('max_tokens', 1200), stream=False)
        if settings.get('thinking') != 'disabled' and settings.get('reasoning_effort') in ('low', 'high', 'max'):
            payload['reasoning_effort'] = settings['reasoning_effort']
        if request.tools:
            payload.update(tools=deepcopy(request.tools), tool_choice=deepcopy(request.tool_choice or 'auto'))
        return payload

    def decode(self, response):
        choices = response.get('choices') if isinstance(response, dict) else None
        if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get('message'), dict)):
            raise ModelError('invalid_response')
        message = choices[0]['message']
        # Do not let a malformed provider body become an AttributeError carrying
        # request context later in chat()/output formatting.
        if message.get('content') is not None and not isinstance(message['content'], str):
            raise ModelError('invalid_response')
        if message.get('tool_calls') is not None and not isinstance(message['tool_calls'], list):
            raise ModelError('invalid_response')
        raw_usage = response.get('usage')
        usage = {k: v for k, v in raw_usage.items() if k in
                 ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0} if isinstance(raw_usage, dict) else {}
        finish = choices[0].get('finish_reason')
        finish = finish if finish in ('stop', 'length', 'tool_calls', 'content_filter') else None
        return ModelResult(deepcopy(message), finish, usage)

    def assistant_message(self, message):
        # Tool replay is provider-owned. Keep hidden continuation state; it is
        # never used as reply text or copied into telemetry.
        return {'role': 'assistant', **deepcopy({k: message[k] for k in
                ('content', 'tool_calls', 'reasoning_content') if k in message})}
