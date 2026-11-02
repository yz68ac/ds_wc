"""Request-local tool state and explicit results; no queues or durable writes."""
from dataclasses import dataclass, field
import time

from provider import ModelError
from wxbridge.config import InstanceConfig


class ToolError(RuntimeError):
    """A fixed business error code safe to return to the model."""


@dataclass(frozen=True)
class ToolResult:
    output: dict = field(repr=False)
    image_request: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolReply:
    text: str = field(repr=False)
    search: dict
    image_requests: tuple = field(default=(), repr=False)


@dataclass
class ToolContext:
    settings: InstanceConfig = field(repr=False)
    backend: str
    deadline: float
    allowed_tools: frozenset[str]
    search_client: object = field(default=None, repr=False)
    on_search: object = field(default=None, repr=False)
    check: object = field(default=None, repr=False)
    sources: list = field(default_factory=list, repr=False)
    errors: list = field(default_factory=list)
    seen_ids: set = field(default_factory=set, repr=False)
    image_requests: list = field(default_factory=list, repr=False)
    calls: int = 0
    actions: int = 0
    attempted: bool = False
    notified: bool = False

    def check_active(self):
        if self.check is not None:
            self.check()
        if time.monotonic() >= self.deadline:
            raise ModelError('deadline_exceeded', retryable=True)

    @property
    def remaining_search_calls(self):
        return max(0, int(self.settings.search.max_requests)
                   - (self.actions if self.backend == 'native' else self.calls))

    def search_started(self):
        self.attempted = True
        if not self.notified:
            self.notified = True
            if self.on_search is not None:
                self.on_search()

    def metadata(self):
        value = dict(attempted=self.attempted, calls=self.calls, results=len(self.sources),
                     errors=list(dict.fromkeys(self.errors)))
        if self.backend == 'native':
            value.update(backend='native', actions=self.actions)
        return value
