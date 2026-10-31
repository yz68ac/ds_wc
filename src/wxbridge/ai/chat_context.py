"""Shared chat assembly for reply and participation requests; no I/O."""
from dataclasses import dataclass, replace

from wxbridge.ai.context import ContextRows, context_stats, select_context, source_anchor
from wxbridge.ai.chat_payload import chat_payload, encode_payload, fit_payload


@dataclass(frozen=True)
class ChatContext:
    selection: ContextRows
    payload: dict

    @classmethod
    def build(cls, cfg, rows, triggers, title, *, account='', profile=None, memory=None):
        history = select_context(cfg, rows, source_anchor(triggers), triggers=triggers, account=account)
        memory_enabled = cfg.get('memory', {}).get('enabled', False)
        payload = chat_payload([*history, *triggers], title, [m['local_id'] for m in triggers],
                               account=account, profile=profile, memory_enabled=memory_enabled)
        if memory_enabled and (memory or {}).get('text'):
            payload['memory'] = memory['text'].splitlines()
        return cls(history, payload)

    def fit(self, cfg):
        target = cfg['target']
        return replace(self, payload=fit_payload(self.payload, target['context_max_chars'],
            strategy=target.get('context_budget_strategy', 'recent'), plan=self.selection))

    @property
    def text(self):
        return encode_payload(self.payload)

    @property
    def stats(self):
        return context_stats(self.selection, self.payload)
