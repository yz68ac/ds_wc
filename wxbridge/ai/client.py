"""Role-facing model facade; adapters own wire formats, queues own retries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
import urllib.error
import urllib.request

from wxbridge.config import deepseek_api_key
from wxbridge.ai.roles import model_settings
from wxbridge.ai.providers import ModelError, ModelRequest, ModelResult, get_adapter

MAX_REQUEST_BYTES = 48 * 1024 * 1024


_metrics = ContextVar('model_metrics', default=None)


@contextmanager
def capture_metrics():
    values = []
    token = _metrics.set(values)
    try:
        yield values
    finally:
        _metrics.reset(token)


class ModelClient:
    def __init__(self, cfg, *, role='reply', adapter=None):
        self.cfg = model_settings(cfg, role)
        self.role = role
        self.adapter = adapter if adapter is not None else get_adapter(self.cfg.get('provider', 'deepseek'))
        self.api_key = deepseek_api_key({'deepseek': self.cfg})
        if not self.api_key:
            raise ModelError('missing_api_key')
        self.base_url = self.cfg['base_url'].rstrip('/')

    def _post(self, payload, timeout):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(body) > MAX_REQUEST_BYTES:
            raise ModelError('request_too_large')
        req = urllib.request.Request(self.adapter.endpoint(self.base_url), data=body,
            headers=self.adapter.headers(self.api_key), method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(8*1024*1024+1)
            if len(body) > 8*1024*1024:
                raise ModelError('response_too_large')
            return json.loads(body.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ModelError('http_'+str(status), retryable=status in (408, 429) or status >= 500,
                             status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError('connection_failed', retryable=True) from None
        except (ValueError, UnicodeError):
            raise ModelError('invalid_response') from None

    def request(self, messages, *, tools=None, tool_choice=None, deadline=None) -> ModelResult:
        started, usage, finish, error = time.monotonic(), {}, None, ''
        try:
            timeout = float(self.cfg.get('timeout_seconds', 60))
            remaining = deadline-time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise ModelError('deadline_exceeded', retryable=True)
            payload = self.adapter.encode(self.cfg, ModelRequest(messages, tools, tool_choice))
            remaining = deadline-time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise ModelError('deadline_exceeded', retryable=True)
            data = self._post(payload, min(timeout, remaining))
            result = self.adapter.decode(data)
            usage, finish = result.usage, result.finish_reason
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
        code = ''
        if require_complete and result.finish_reason == 'length':
            code = 'output_limit'
        elif require_complete and result.finish_reason == 'content_filter':
            code = 'output_filtered'
        elif require_complete and not result.message.get('tool_calls') and not (result.message.get('content') or '').strip():
            code = 'empty_reply'
        if code:
            values = _metrics.get()
            if values:
                values[-1]['error'] = code
            raise ModelError(code, retryable=code != 'output_filtered')
        return result.message

    def chat(self, messages):
        return (self.complete(messages, require_complete=True).get('content') or '').strip()

    def assistant_message(self, message):
        """Prepare a tool-continuation turn without exposing provider logic."""
        return self.adapter.assistant_message(message)


# Compatibility imports; new business code uses the provider-neutral names.
DeepSeekClient = ModelClient
DeepSeekError = ModelError
