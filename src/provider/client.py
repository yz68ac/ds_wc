"""One resolved model invocation; adapters own formats and callers own retries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import math
import time

from .adapters import ModelError, ModelRequest, ModelResult, get_protocol_adapter
from .transport import send, send_images
from .sdk_transport import create_sdk_client
from .images import ImageRequest, ImageResult, IMAGE_PROTOCOLS, build_image_payload, parse_image_response

_metrics = ContextVar('model_metrics', default=None)


@contextmanager
def capture_metrics():
    values = []
    token = _metrics.set(values)
    try:
        yield values
    finally:
        _metrics.reset(token)


class Client:
    def __init__(self, settings, *, api_key, role='reply', adapter=None, name=None):
        self.cfg = deepcopy(settings)
        self.role = role
        self.protocol = self.cfg.get('provider', 'deepseek')
        self.adapter = adapter if adapter is not None else get_protocol_adapter(self.protocol)
        self.api_key = api_key
        if not self.api_key:
            raise ModelError('missing_api_key')
        self.base_url = self.cfg['base_url'].rstrip('/')
        self.name = name or {'openai_responses': 'openai', 'anthropic_messages': 'anthropic'}.get(self.protocol, self.protocol)
        self._bound_connection_fingerprint = self._connection_fingerprint(self.cfg, api_key)
        self._sdk, self._sdk_client = create_sdk_client(self.protocol, self.base_url, api_key, self.cfg,
                                                       self.cfg.get('timeout_seconds', 60))
        self._closed = False

    @staticmethod
    def _connection_fingerprint(settings, api_key):
        protocol = settings.get('provider', 'deepseek')
        protocol = {'anthropic': 'anthropic_messages', 'openai': 'openai_compatible'}.get(protocol, protocol)
        base_url = settings['base_url'].rstrip('/')
        if protocol == 'anthropic_messages' and base_url.endswith('/v1'):base_url = base_url[:-3]
        values = [protocol, base_url, api_key,
                  settings.get('proxy_url'), settings.get('anthropic_version'), settings.get('anthropic_auth')]
        return hashlib.sha256(json.dumps(values).encode('utf-8')).hexdigest()

    def check_connection(self, settings, *, api_key):
        if self._bound_connection_fingerprint != self._connection_fingerprint(settings, api_key):
            raise ModelError('client_connection_mismatch')

    def _ensure_open(self):
        if getattr(self, '_closed', False):
            raise ModelError('client_closed')

    def close(self):
        if not self._closed:
            self._sdk_client.close()
            self._closed = True

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def fetch_models(self, *, timeout=30):
        from .discovery import fetch_models
        self._ensure_open()
        return fetch_models(self._sdk, self._sdk_client, self.name, timeout=timeout)

    # Compatibility alias; fetch_models() explicitly performs network I/O.
    list_models = fetch_models

    def generate(self, request: ModelRequest, *, deadline=None, require_complete=False):
        if not isinstance(request.model, str) or not request.model.strip():
            raise ModelError('missing_model')
        settings = {**self.cfg, 'model': request.model}
        return self._generate(request, settings, deadline=deadline, require_complete=require_complete)

    def generate_images(self, request: ImageRequest, *, deadline=None) -> ImageResult:
        """Generate images using this connection's existing OpenAI SDK instance."""
        started, usage, error = time.monotonic(), {}, ''
        try:
            self._ensure_open()
            if self.protocol not in IMAGE_PROTOCOLS:
                raise ModelError('unsupported_image_generation')
            payload, timeout = build_image_payload(request)
            if deadline is not None:
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    raise ModelError('invalid_deadline')
                remaining = deadline-time.monotonic()
                if remaining <= 0:raise ModelError('deadline_exceeded', retryable=True)
                timeout = min(timeout, remaining)
            response = send_images(self._sdk, self._sdk_client, payload, timeout)
            result = parse_image_response(response, expected_count=payload.get('n', 1))
            usage = result.usage
            return result
        except ModelError as exc:
            error = exc.code
            raise
        finally:
            if _metrics.get() is not None:
                _metrics.get().append(dict(role=self.role, provider=self.protocol, operation='image_generation',
                    elapsed_ms=round((time.monotonic()-started)*1000, 2), finish_reason=None, usage=usage, error=error))

    def _generate(self, request, settings, *, deadline=None, require_complete=False):
        allowed = {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort',
                   'thinking_mode', 'thinking_budget_tokens'}
        if not isinstance(request.options, dict) or set(request.options) - allowed:
            raise ModelError('invalid_model_options')
        settings = {**settings, **request.options}
        for key, lower, upper in (('timeout_seconds', 1, 300), ('max_tokens', 1, 128000), ('temperature', 0, 2)):
            value = settings.get(key)
            if value is None:
                if key in settings and key != 'temperature':raise ModelError('invalid_model_options')
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise ModelError('invalid_model_options')
            if key == 'max_tokens' and type(value) is not int:raise ModelError('invalid_model_options')
        if settings.get('thinking', 'disabled') not in ('enabled', 'disabled'):
            raise ModelError('invalid_model_options')
        if settings.get('reasoning_effort') not in (None, 'none', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ModelError('invalid_model_options')
        route = hashlib.sha256((self._bound_connection_fingerprint + settings['model']).encode('utf-8')).hexdigest()
        messages = deepcopy(request.messages)
        for message in messages:
            if isinstance(message, dict):
                previous = message.pop('_provider_route', None)
                if previous is not None and previous != route:raise ModelError('continuation_provider_mismatch')
                state = message.get('_provider_state')
                if isinstance(state, dict) and state.get('route') not in (None, route):
                    raise ModelError('continuation_provider_mismatch')
        result = self._request(messages, settings=settings, tools=request.tools, tool_choice=request.tool_choice,
                               native_search=request.native_search, deadline=deadline, require_complete=require_complete)
        state = result.message.get('_provider_state')
        if isinstance(state, dict):state['route'] = route
        if result.message.get('tool_calls'):result.message['_provider_route'] = route
        return result

    def stream(self, request, *, deadline=None):
        raise ModelError('unsupported_stream')

    def _post(self, payload, timeout):
        self._ensure_open()
        return send(self._sdk, self._sdk_client, self.protocol, payload, timeout)

    def request(self, messages, *, tools=None, tool_choice=None, deadline=None, native_search=None,
                require_complete=False) -> ModelResult:
        return self._request(messages, settings=self.cfg, tools=tools, tool_choice=tool_choice, deadline=deadline,
                             native_search=native_search, require_complete=require_complete)

    def _request(self, messages, *, settings, tools=None, tool_choice=None, deadline=None, native_search=None,
                 require_complete=False) -> ModelResult:
        started, usage, finish, error = time.monotonic(), {}, None, ''
        try:
            self._ensure_open()
            if not isinstance(settings.get('model'), str) or not settings['model'].strip():
                raise ModelError('missing_model')
            timeout = float(settings.get('timeout_seconds', 60))
            remaining = deadline-time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise ModelError('deadline_exceeded', retryable=True)
            if native_search is not None:
                if settings.get('native_search_supported') is False or not getattr(self.adapter, 'native_search_supported', False):
                    raise ModelError('unsupported_native_search')
            payload = self.adapter.build_payload(settings, ModelRequest(messages, tools, tool_choice, native_search))
            remaining = deadline-time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise ModelError('deadline_exceeded', retryable=True)
            data = self._post(payload, min(timeout, remaining))
            result = self.adapter.parse_response(data)
            usage, finish = result.usage, result.finish_reason
            if require_complete:self.check_complete(result, record_error=False)
            return result
        except ModelError as exc:
            error = exc.code
            raise
        finally:
            if _metrics.get() is not None:
                _metrics.get().append(dict(role=getattr(self, 'role', 'reply'),
                    provider=self.cfg.get('provider', 'deepseek'), elapsed_ms=round((time.monotonic()-started)*1000, 2),
                    finish_reason=finish, usage=usage, error=error))

    def complete(self, messages, *, require_complete=False, **kwargs) -> dict:
        result = self.request(messages, **kwargs)
        if require_complete:self.check_complete(result)
        return result.message

    @staticmethod
    def check_complete(result, *, record_error=True):
        code = ''
        if result.finish_reason == 'length':
            code = 'output_limit'
        elif result.finish_reason == 'content_filter':
            code = 'output_filtered'
        elif result.finish_reason == 'pause_turn':
            code = 'incomplete_response'
        elif not result.message.get('tool_calls') and not (result.message.get('content') or '').strip():
            code = 'empty_reply'
        if code:
            values = _metrics.get()
            if values and record_error:
                values[-1]['error'] = code
            raise ModelError(code, retryable=code != 'output_filtered')

    def chat(self, messages):
        return (self.complete(messages, require_complete=True).get('content') or '').strip()

    def assistant_message(self, message):
        """Prepare a tool-continuation turn without exposing provider logic."""
        result = self.adapter.assistant_message(message)
        if '_provider_route' in message:result['_provider_route'] = message['_provider_route']
        return result


# Compatibility name for existing bridge integrations.
ModelClient = Client
