"""One account owns the reader, model budget and ordered submission gate."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from wechat_linux.storage.store import WeChatStore
from wxbridge.instances import queue_candidates


class AccountRuntime:
    def __init__(self, cfg, *, store=None):
        self.cfg = cfg
        self.store = store if store is not None else WeChatStore(cfg['db_dir'], cfg['keys_file'], cfg['decrypted_dir'])
        self.sessions = []
        self.inactive = []
        self.send_gap = cfg.get('account', {}).get('send_interval_seconds', 1)
        active = {Path(e['config']['queue']['file']) for e in cfg.get('instances', []) if e['enabled']}
        root = Path(cfg['queue']['file'])
        from wxbridge.storage.journal import open_readonly
        try:
            for path in sorted(set(queue_candidates(cfg))-active):
                if not path.exists():
                    continue
                journal = open_readonly(str(path))
                item = SimpleNamespace(instance_id=path.parent.name if path != root else 'legacy', journal=journal, ui_job=None)
                self.inactive.append(item)
                identity = journal.get('identity') or {}
                if identity.get('account') != self.store.self_wxid:
                    raise ValueError('inactive_queue_account_mismatch')
        except BaseException:
            self.close()
            raise

    def histories(self, *, requests_only=False):
        values = [*self.sessions, *self.inactive]
        if requests_only:
            values = [s for s in values if s.journal.has_external_history()]
        return values

    def close(self):
        for item in self.inactive:
            item.journal.close()
        self.inactive.clear()

    def occupied(self):
        return sum(sum(s.worker_counts().values()) for s in self.sessions)



    def sending(self):
        for session in self.sessions:
            if session.send_in_progress():
                return session
        for session in self.inactive:
            if session.journal.has_intent():
                return session
        return None

    def can_send(self, session):
        if self.sending() is not None:
            return False
        head = session.journal.head()
        if head and head.get('trigger_kind') == 'proactive':
            if any(s.has_foreground_tasks() for s in self.sessions):
                return False
        latest = max((s.journal.get('last_success', 0) for s in self.sessions), default=0)
        return time.time()-latest >= self.send_gap

    def refresh_before_send(self):
        for session in self.sessions:
            session.intake(force=True)
            session.prioritize()

    def status(self):
        owner = self.sending()
        return dict(model_workers=self.occupied(), model_capacity=None,
                    send_owner=owner.instance_id if owner else None,
                    send_blocked=bool(owner and (owner in self.inactive or owner.send_blocked())))
