"""Public scheduling phases shared by single-group and account coordinators.

The session still owns its business controllers. Account scheduling uses this
small surface instead of reaching into their worker tuples or private methods.
"""
from __future__ import annotations

import time


class SessionPhases:
    def intake(self, *, force=False):
        return self._poll_messages(force=force)

    def prioritize(self):
        if self.proactive:
            self.proactive.priority(time.time())

    def collect_background_results(self):
        with self.metrics.measure('memory'):
            self._memory_step(collect_only=True)

    def collect_results(self):
        with self.metrics.measure('generation'):
            self._poll_generation_results()
        with self.metrics.measure('ui_result'):
            self._ui_result_step()
        with self.metrics.measure('confirmation'):
            self._confirm_step()
        # Apply completed work before a new foreground request can preempt it.
        self.collect_background_results()
        self._participation_collected = bool(self.proactive and self.proactive.addressed_judgment())
        if self._participation_collected:
            # Explicit invitations are accepted into the durable foreground
            # queue once judged, even when a newer real mention is waiting.
            # Source identity and the original TTL are still checked there.
            with self.metrics.measure('participation'):
                self.proactive.step(time.time(), collect_only=True)

    def start_foreground(self, *, max_new=None):
        with self.metrics.measure('generation'):
            return self._start_generations(max_new=max_new)

    def participate(self):
        if getattr(self, '_participation_collected', False):
            self._participation_collected = False
            return
        if self.proactive:
            with self.metrics.measure('participation'):
                self.proactive.step(time.time())

    def deliver(self):
        with self.metrics.measure('delivery'):
            self._send_step()

    def background(self):
        with self.metrics.measure('memory'):
            self._memory_step()

    def worker_counts(self):
        return dict(generation=len(self.generators), memory=int(self.memory_job is not None),
                    participation=int(bool(self.proactive and self.proactive.job)))

    def foreground_pending(self, now=None):
        head = self.journal.head()
        if head and head.get('blocked_reason'):
            return False
        now = time.time() if now is None else now
        return any(t['generation'] == 'pending' and now >= t.get('generation_retry_at', 0)
                   and t.get('trigger_kind') != 'proactive' for t in self.journal.tasks())

    def has_foreground_tasks(self):
        return any(t.get('trigger_kind') != 'proactive' for t in self.journal.tasks())



    def send_in_progress(self):
        return self.ui_job is not None or self.journal.has_intent()

    def send_blocked(self):
        head = self.journal.head()
        return bool(head and head.get('intent') and head.get('blocked_reason'))

    def run_phases(self, gateway=None):
        """The one-session schedule; multi-group uses the same phase methods."""
        self.intake()
        if gateway:
            gateway.tick()
        self.collect_results()
        self.prioritize()
        self.start_foreground()
        self.participate()
        self.deliver()
        self.background()
        self.maintain()
