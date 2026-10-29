"""Durable candidate decisions; only the group owner applies worker results."""
from __future__ import annotations

import json
import time

from wxbridge.ai.context import ContextError, message_time
from wxbridge.ai.participation import build_prompt, parse_decision
from wxbridge.ai.roles import model_settings
from wxbridge.app.task_policy import participation_failure_code
from wxbridge.app.workers import WorkerLease, WorkerCleanupError
from wechat_linux.messages.pats import is_pat


class ParticipationController:
    def __init__(self, session):
        self.bot, self.cfg = session, session.cfg['participation']
        self.private = not session.chat_id.endswith('@chatroom')
        self.job, self.pending, self.started = None, None, None
        self.bot.journal.migrate_participation(time.time(), self.bot.cfg['target']['max_message_age_seconds'])
        self.update(enabled=self.cfg['enabled'], decision_required=not self.private, outcome='waiting', error='')
        self.priority(time.time())

    def update(self, **fields):
        journal = self.bot.journal
        with journal.db:
            journal.set('participation', {**journal.get('participation', {}), **fields})

    def expire_reply(self, task, now):
        if (task.get('trigger_kind') != 'participation' or task['intent']
                or task.get('next_chunk', 0) > 0 or now < task.get('reply_expires_at', float('inf'))):
            return False
        with self.bot.journal.db:
            task.update(phase='done', version=task['version']+1, chunks=[], triggers=[],
                        cancelled='reply_expired', completed_at=now)
            self.bot.journal.put(task)
        return True

    def priority(self, now):
        for task in self.bot.journal.tasks():
            self.expire_reply(task, now)
        if not self.cfg['enabled']:
            self.cancel('disabled')
            self.bot.journal.cancel_participation(now, code='disabled')
            with self.bot.journal.db:
                for task in self.bot.journal.tasks():
                    if task.get('trigger_kind') == 'participation' and not task['intent']:
                        task.update(phase='done', version=task['version']+1, chunks=[], triggers=[],
                                    cancelled='participation_disabled', completed_at=now)
                        self.bot.journal.put(task)

    def _reply_deadline(self, now):
        cfg = self.bot.cfg
        timeout = model_settings(cfg, 'reply')['timeout_seconds']
        if cfg['search']['enabled']:
            timeout = max(timeout, cfg['search']['total_timeout_seconds'])
        if cfg['vision']['enabled'] and cfg['vision'].get('download_enabled', False):
            timeout += cfg['vision']['download_timeout_seconds']
        return now+timeout+30+cfg['ui']['send_confirm_timeout_seconds']

    def cancel(self, reason='stopped'):
        if self.job:
            self.job.close()
            self.job, self.pending, self.started = None, None, None
        self.update(outcome=reason)

    def _collect(self, now):
        if self.job is None:
            return
        result = self.job.poll()
        if result is None:
            return
        if result.get('error') == 'WorkerCleanupError':
            raise WorkerCleanupError()
        self.bot.metrics.record_models(result.get('model_metrics'))
        self.job.close()
        snapshot, started = self.pending, self.started
        self.job, self.pending, self.started = None, None, None
        duration = max(0, (time.monotonic()-started)*1000)
        if not result.get('ok'):
            code = participation_failure_code(result)
            self.bot.journal.fail_participation(snapshot, now, code)
            self.update(outcome='error', error=code, last_duration_ms=duration)
            self.bot.log.warning('参与决策失败 category=%s', code)
            return
        try:
            decision = parse_decision(json.dumps(result['decision']), snapshot['source_ids'])
            tasks = self.bot.journal.apply_participation(snapshot, decision['reply_to'], now,
                                                       reply_deadline=self._reply_deadline(now))
        except (KeyError, TypeError, ValueError) as exc:
            code = participation_failure_code(dict(error=getattr(exc, 'code', str(exc))))
            self.bot.journal.fail_participation(snapshot, now, code)
            self.update(outcome='error', error=code, last_duration_ms=duration)
            return
        approved = [identifier for task in tasks for identifier in task['trigger_ids']]
        expired = all(now >= m['time']+self.bot.cfg['target']['max_message_age_seconds']
                      for m in snapshot['messages'])
        self.update(outcome='approved' if tasks else 'expired' if expired else 'silent', error='', last_duration_ms=duration,
            last_decision=dict(at=now, source_ids=snapshot['source_ids'], reply_to=approved,
                               task_ids=[task['id'] for task in tasks]))
        self.bot.log.info('参与决策完成 candidates=%s selected=%s tasks=%s',
                          len(snapshot['source_ids']), len(approved), len(tasks))

    def _rows(self, snapshot, now):
        seconds = self.bot.cfg['target']['context_window_seconds']
        rows = self.bot.store.messages_in_window(self.bot.chat_id, int(now-seconds), int(now))
        # Reserved originals win over another read of a changing database page.
        current = {m['local_id']: m for m in rows if m.get('local_id')
                   and m['local_id'] <= self.bot.last_local_id and m.get('sender')
                   and message_time(m) is not None and now-seconds <= m['time'] <= now
                   and (m.get('type') == 1 or is_pat(m))}
        current.update({m['local_id']: m for m in snapshot['messages']})
        return sorted(current.values(), key=lambda m: (m['time'], m['local_id']))

    def step(self, now, *, collect_only=False):
        self.priority(now)
        if not self.cfg['enabled']:
            return
        self._collect(now)
        if collect_only or self.job is not None:
            return
        if not self.private:
            channel = self.bot.models.status()['background']
            if channel['running'] or channel['queued']:
                self.update(outcome='waiting_background')
                return
        limit = self.cfg['max_batch_messages']
        while limit:
            snapshot = self.bot.journal.reserve_participation(now,
                debounce_seconds=self.cfg['debounce_seconds'], max_wait_seconds=self.cfg['max_wait_seconds'],
                max_batch_messages=limit)
            if snapshot is None:
                return
            if self.private or self.bot.echo:
                tasks = self.bot.journal.apply_participation(snapshot, snapshot['source_ids'], now,
                                                           reply_deadline=self._reply_deadline(now))
                self.update(outcome='reply_queued' if tasks else 'expired', error='',
                    last_dispatch=dict(at=now, source_ids=snapshot['source_ids'],
                                       task_ids=[task['id'] for task in tasks]))
                return
            try:
                rows = self._rows(snapshot, now)
                prompt = build_prompt(self.bot.cfg, rows, self.bot.chat_title, source_ids=snapshot['source_ids'],
                    account=self.bot.store.self_wxid, profile=self.bot.profile_snapshot(rows))
            except ContextError as exc:
                if str(exc) == 'participation_context_too_large' and len(snapshot['source_ids']) > 1:
                    self.bot.journal.release_participation(snapshot, now)
                    limit = max(1, len(snapshot['source_ids'])//2)
                    continue
                self.bot.journal.fail_participation(snapshot, now, 'ContextError', retry=False)
                self.update(outcome='error', error='ContextError')
                return
            except Exception as exc:
                code = participation_failure_code(dict(error=getattr(exc, 'code', type(exc).__name__)))
                self.bot.journal.fail_participation(snapshot, now, code)
                self.update(outcome='error', error=code)
                return
            timeout = model_settings(self.bot.cfg, 'participation')['timeout_seconds']
            try:
                with WorkerLease(self.bot.job_factory('participation', (self.bot.cfg, prompt,
                    dict(source_ids=snapshot['source_ids'])), timeout=timeout)) as lease:
                    self.update(outcome='evaluating', error='', started_at=now)
                    self.pending, self.started, self.job = snapshot, time.monotonic(), lease.job
                    lease.transfer()
            except WorkerCleanupError:
                raise
            except Exception as exc:
                self.bot.journal.fail_participation(snapshot, now,
                    participation_failure_code(dict(error=getattr(exc, 'code', type(exc).__name__))))
                self.update(outcome='error', error='participation_worker_failed')
                return
            self._collect(now)
            return
