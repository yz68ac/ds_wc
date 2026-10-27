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
        self.capacity = cfg['queue']['generation_concurrency']
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
            values = [s for s in values if s.journal.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_requests'").fetchone()]
        return values

    def close(self):
        for item in self.inactive:
            item.journal.close()
        self.inactive.clear()

    def occupied(self):
        return sum(len(s.generators) + int(s.memory_job is not None) +
                   int(bool(s.proactive and s.proactive.job)) for s in self.sessions)

    def foreground_pending(self):
        now = time.time()
        for session in self.sessions:
            head = session.journal.head()
            if head and head.get('blocked_reason'):
                continue
            if any(t['generation'] == 'pending' and now >= t.get('generation_retry_at', 0)
                   and t.get('trigger_kind') != 'proactive' for t in session.journal.tasks()):
                return True
        return False

    def can_start(self, session, kind):
        if kind == 'generate' and self.occupied() >= self.capacity:
            # Only pending work is cancelled. Existing send intents are unrelated
            # to API scheduling and are never reset or replayed here.
            for other in self.sessions:
                if self.occupied() < self.capacity:
                    break
                other._pause_memory()
            for other in self.sessions:
                if self.occupied() < self.capacity:
                    break
                if (other.proactive and other.proactive.job
                        and (other.proactive.pending or {}).get('reason') != 'addressed'):
                    other.proactive.cancel('foreground_priority')
        if self.occupied() >= self.capacity:
            return False
        if kind in ('memory', 'proactive') and self.foreground_pending():
            return False
        if kind == 'memory' and any(s.memory_job for s in self.sessions):
            return False
        return True

    def sending(self):
        for session in self.histories():
            if session.ui_job or any(t.get('intent') for t in session.journal.tasks()):
                return session
        return None

    def can_send(self, session):
        if self.sending() is not None:
            return False
        head = session.journal.head()
        if head and head.get('trigger_kind') == 'proactive':
            if any(t.get('trigger_kind') != 'proactive' for s in self.sessions for t in s.journal.tasks()):
                return False
        latest = max((s.journal.get('last_success', 0) for s in self.sessions), default=0)
        return time.time()-latest >= self.send_gap

    def refresh_before_send(self):
        for session in self.sessions:
            session._poll_messages(force=True)
            if session.proactive:
                session.proactive.priority(time.time())

    def status(self):
        owner = self.sending()
        return dict(model_workers=self.occupied(), model_capacity=self.capacity,
                    send_owner=owner.instance_id if owner else None,
                    send_blocked=bool(owner and (owner in self.inactive or any(t.get('blocked_reason') and t.get('intent')
                                                  for t in owner.journal.tasks()))))
