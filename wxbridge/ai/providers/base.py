"""Small provider contract; no bot configuration, credentials, I/O or retries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ModelError(RuntimeError):
    def __init__(self, code, *, retryable=False, status=None):
        super().__init__(code)
        self.code, self.retryable, self.status = code, retryable, status


@dataclass(frozen=True)
class ModelRequest:
    # Keep the existing internal message/tool shape. Other wire protocols must
    # translate it inside their adapter, not in prompts or the tool executor.
    messages: list[dict] = field(repr=False)
    tools: list[dict] | None = field(default=None, repr=False)
    tool_choice: str | dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ModelResult:
    message: dict = field(repr=False)
    finish_reason: str | None
    usage: dict


class ProviderAdapter(Protocol):
    def endpoint(self, base_url: str) -> str: ...
    def headers(self, api_key: str) -> dict[str, str]: ...
    def encode(self, settings: dict, request: ModelRequest) -> dict: ...
    def decode(self, response: object) -> ModelResult: ...
    def assistant_message(self, message: dict) -> dict: ...
