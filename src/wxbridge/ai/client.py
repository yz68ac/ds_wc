"""Request-local views over group-owned provider clients."""
from contextvars import ContextVar
from provider import ModelError, ModelRequest, capture_metrics
from wxbridge.ai.roles import model_settings
_scope = ContextVar('group_model_scope')

def client_for(role):
    lane, cfg, job, metrics = _scope.get()
    return RoleRequest(lane, cfg, role, job, metrics)

class RoleRequest:

    def __init__(self, lane, cfg, role, job, metrics):
        self.lane, self.role, self.job, self.metrics = (lane, role, job, metrics)
        self.cfg = model_settings(cfg, role, provider_config=lane.config)

    @property
    def adapter(self):
        return self.lane.get_client().adapter

    def request(self, messages, *, tools=None, tool_choice=None, native_search=None, deadline=None, require_complete=False):
        self.job.check()
        before = len(self.metrics)
        options = {k: v for k, v in self.cfg.items() if k in {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort', 'thinking_mode', 'thinking_budget_tokens'}}
        try:
            result = self.lane.get_client().generate(ModelRequest(messages, tools, tool_choice, native_search, model=self.cfg['model'], options=options), deadline=min(deadline, self.job.deadline) if deadline else self.job.deadline, require_complete=require_complete)
            self.job.check()
            return result
        finally:
            for entry in self.metrics[before:]:
                entry.update(role=self.role, channel=self.lane.name)

    def complete(self, messages, *, require_complete=False, **kwargs):
        return self.request(messages, require_complete=require_complete, **kwargs).message

    def chat(self, messages):
        return (self.complete(messages, require_complete=True).get('content') or '').strip()

    def assistant_message(self, message):
        return self.lane.get_client().assistant_message(message)

    @staticmethod
    def check_complete(result):
        from provider import Client
        Client.check_complete(result)
