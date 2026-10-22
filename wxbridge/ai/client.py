"""按角色调用 Chat Completions；重试交给持久队列，错误不携带请求正文。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.request

from wxbridge.config import deepseek_api_key
from wxbridge.ai.roles import model_settings

MAX_REQUEST_BYTES = 48 * 1024 * 1024


class DeepSeekError(RuntimeError):
    def __init__(self, code, *, retryable=False, status=None):
        super().__init__(code)
        self.code, self.retryable, self.status = code, retryable, status


@dataclass(frozen=True)
class ModelResult:
    message: dict
    finish_reason: str | None
    usage: dict


_metrics = ContextVar('model_metrics', default=None)


@contextmanager
def capture_metrics():
    values = []
    token = _metrics.set(values)
    try:
        yield values
    finally:
        _metrics.reset(token)


class DeepSeekClient:
    def __init__(self, cfg, *, role='reply'):
        self.cfg = model_settings(cfg, role)
        self.role = role
        self.api_key = deepseek_api_key({'deepseek': self.cfg})
        if not self.api_key:
            raise DeepSeekError('missing_api_key')
        self.base_url = self.cfg['base_url'].rstrip('/')

    def _post(self, payload, timeout):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(body) > MAX_REQUEST_BYTES:
            raise DeepSeekError('request_too_large')
        req = urllib.request.Request(self.base_url+'/chat/completions', data=body,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+self.api_key}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(8*1024*1024+1)
            if len(body) > 8*1024*1024:
                raise DeepSeekError('response_too_large')
            return json.loads(body.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            raise DeepSeekError('http_'+str(exc.code), retryable=exc.code in (408, 429) or exc.code >= 500,
                                status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeepSeekError('connection_failed', retryable=True) from None
        except (ValueError, UnicodeError):
            raise DeepSeekError('invalid_response') from None

    def request(self, messages, *, tools=None, tool_choice=None, deadline=None) -> ModelResult:
        payload = dict(model=self.cfg['model'], messages=messages, temperature=self.cfg.get('temperature', 1.0),
                       max_tokens=self.cfg.get('max_tokens', 1200), stream=False)
        if self.cfg.get('provider', 'deepseek') == 'deepseek' and self.cfg.get('thinking') in ('enabled', 'disabled'):
            payload['thinking'] = {'type': self.cfg['thinking']}
        if self.cfg.get('thinking') != 'disabled' and self.cfg.get('reasoning_effort') in ('low', 'high', 'max'):
            payload['reasoning_effort'] = self.cfg['reasoning_effort']
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice or 'auto')
        started, usage, finish, error = time.monotonic(), {}, None, ''
        try:
            timeout = float(self.cfg.get('timeout_seconds', 60))
            remaining = deadline-time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise DeepSeekError('deadline_exceeded', retryable=True)
            data = self._post(payload, min(timeout, remaining))
            choices = data.get('choices') if isinstance(data, dict) else None
            if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                    or not isinstance(choices[0].get('message'), dict)):
                raise DeepSeekError('invalid_response')
            raw_usage = data.get('usage')
            if isinstance(raw_usage, dict):
                usage = {k: v for k, v in raw_usage.items() if k in
                         ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}
            finish = choices[0].get('finish_reason')
            finish = finish if finish in ('stop', 'length', 'tool_calls', 'content_filter') else None
            return ModelResult(choices[0]['message'], finish, usage)
        except DeepSeekError as exc:
            error = exc.code
            raise
        finally:
            if _metrics.get() is not None:
                _metrics.get().append(dict(role=getattr(self, 'role', 'reply'),
                    provider=self.cfg.get('provider', 'deepseek'), elapsed_ms=round((time.monotonic()-started)*1000, 2),
                    finish_reason=finish, usage=usage, error=error))

    def complete(self, messages, *, require_complete=False, **kwargs) -> dict:
        result = self.request(messages, **kwargs)
        if require_complete and result.finish_reason == 'length':
            values = _metrics.get()
            if values:
                values[-1]['error'] = 'output_limit'
            raise DeepSeekError('output_limit', retryable=True)
        return result.message

    def chat(self, messages):
        return (self.complete(messages).get('content') or '').strip()


ModelClient = DeepSeekClient  # 保留已有导入入口；业务调用按 role 隔离配置。
