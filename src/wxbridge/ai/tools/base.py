"""Request-local tool state and explicit results; no queues or durable writes."""
from dataclasses import dataclass, field
from contextlib import contextmanager
import time

from provider import ModelError
from wxbridge.config import InstanceConfig
from wxbridge.contracts.image_jobs import ImageProposal, QuotedImageInput


class ToolError(RuntimeError):
    """A fixed business error code safe to return to the model."""


@dataclass(frozen=True)
class ToolResult:
    output: dict = field(repr=False)
    image_request: ImageProposal | None = field(default=None, repr=False)
    # Explicitly authored by the tool, not inferred from model or network output.
    user_notice: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolReply:
    text: str = field(repr=False)
    search: dict
    image_proposals: tuple[ImageProposal, ...] = field(default=(), repr=False)


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
    image_proposals: list[ImageProposal] = field(default_factory=list, repr=False)
    edit_images: dict[str, QuotedImageInput] = field(default_factory=dict, repr=False)
    calls: int = 0
    actions: int = 0
    attempted: bool = False
    notified: bool = False
    search_elapsed: float = 0
    tool_notices: list[str] = field(default_factory=list, repr=False)

    def check_active(self):
        if self.check is not None:
            self.check()
        if time.monotonic() >= self.deadline:
            raise ModelError('deadline_exceeded', retryable=True)

    @property
    def remaining_search_calls(self):
        return max(0, int(self.settings.search.max_requests)
                   - (self.actions if self.backend == 'native' else self.calls))

    @property
    def remaining_search_seconds(self):
        return max(0.0, float(self.settings.search.total_timeout_seconds) - self.search_elapsed)

    @contextmanager
    def search_operation(self):
        self.check_active()
        remaining = self.remaining_search_seconds
        if remaining <= 0:
            raise ToolError('search_timeout')
        started = time.monotonic()
        deadline = min(self.deadline, started + remaining)
        try:
            yield deadline
            self.check_active()
            if time.monotonic() >= deadline:
                raise ToolError('search_timeout')
        finally:
            self.search_elapsed += max(0.0, time.monotonic() - started)

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
