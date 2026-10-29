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
        self.participation.priority(time.time())

    def collect_background_results(self):
        with self.metrics.measure('memory'):
            self._memory_step(collect_only=True)
        if self.profile:
            self.profile.step(collect_only=True)

    def collect_results(self):
        with self.metrics.measure('generation'):
            self._poll_generation_results()
        with self.metrics.measure('ui_result'):
            self._ui_result_step()
        with self.metrics.measure('confirmation'):
            self._confirm_step()
        # Apply completed work before a new foreground request can preempt it.
        self.collect_background_results()
        with self.metrics.measure('participation'):
            self.participation.step(time.time(), collect_only=True)

    def start_foreground(self, *, max_new=None):
        with self.metrics.measure('generation'):
            return self._start_generations(max_new=max_new)

    def participate(self):
        with self.metrics.measure('participation'):
            self.participation.step(time.time())
        # Immediate diagnostics/fakes may complete a decision in the same turn.
        self.start_foreground()

    def deliver(self):
        with self.metrics.measure('delivery'):
            self._send_step()

    def background(self):
        with self.metrics.measure('memory'):
            self._memory_step()
        if self.profile:
            self.profile.step()

    def worker_counts(self):
        return dict(generation=len(self.generators), memory=int(self.memory_job is not None),
                    participation=int(self.participation.job is not None),
                    profile=int(bool(self.profile and self.profile.job)))

    def foreground_pending(self, now=None):
        head = self.journal.head()
        if head and head.get('blocked_reason'):
            return False
        now = time.time() if now is None else now
        return any(t['generation'] == 'pending' and now >= t.get('generation_retry_at', 0)
                   for t in self.journal.tasks())

    def has_foreground_tasks(self):
        return bool(self.journal.tasks())



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
