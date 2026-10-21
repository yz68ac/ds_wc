"""低优先级主动接话调度；主进程查询，工作进程判断，统一队列发送。"""
from __future__ import annotations

import json
import logging

from wxbridge.ai.proactive import activity, build_prompt, parse_decision
from wxbridge.ai.memory import member_labels

log = logging.getLogger('wxbridge')


class ProactiveController:
    def __init__(self, bot):
        self.bot, self.cfg, self.job = bot, bot.cfg['proactive'], None
        if bot.journal.get('proactive') is None:
            with bot.journal.db:
                bot.journal.set('proactive', dict(last_evaluated_id=bot.last_local_id,
                    evaluations=0, last_evaluated_at=0, last_checked_at=0,
                    last_checked_id=bot.last_local_id, outcome='waiting_new_messages', error=''))
        self.update(enabled=self.cfg['enabled'])
        if self.state().get('outcome') == 'evaluating':
            self.update(outcome='interrupted')

    def state(self):
        return self.bot.journal.get('proactive', {})

    def update(self, **fields):
        with self.bot.journal.db:
            self.bot.journal.set('proactive', {**self.state(), **fields})

    def cancel(self, reason):
        if self.job:
            self.job[0].close()
            self.job = None
            self.update(outcome=reason)

    def foreground(self):
        return any(t.get('trigger_kind') != 'proactive' for t in self.bot.journal.tasks())

    def context(self, now):
        return activity(self.bot.store.messages_in_window(self.bot.chat_id,
            int(now-self.cfg['window_seconds']), int(now)), self.bot.store.self_wxid,
            now, self.cfg['window_seconds'])

    def eligible(self, rows, own, now):
        return (not own and len(rows) >= self.cfg['min_messages']
                and now-rows[-1]['time'] <= self.cfg['reply_ttl_seconds'])

    def validate(self, task, now):
        """每次提交前重新检查；已经存在发送意图的任务只能核对，不取消。"""
        if task.get('trigger_kind') != 'proactive' or task.get('intent'):
            return True
        reason = ''
        if not self.cfg['enabled'] or self.foreground():
            reason = 'foreground_or_disabled'
        elif now > task['proactive']['expires_at']:
            reason = 'expired'
        else:
            try:
                rows, own = self.context(now)
            except Exception as exc:
                self.update(error=type(exc).__name__)
                rows, own = [], False
            if not self.eligible(rows, own, now):
                reason = 'activity_changed'
            elif sum(m['local_id'] > task['proactive']['snapshot_id'] for m in rows) >= self.cfg['min_new_messages']:
                reason = 'context_changed'
        if reason:
            self.bot.journal.discard_proactive(task['id'], reason)
            self.update(outcome=reason)
            return False
        return True

    def priority(self, now):
        if self.foreground() or not self.cfg['enabled']:
            self.cancel('foreground_or_disabled')
            for task in self.bot.journal.tasks():
                if task.get('trigger_kind') == 'proactive' and not task.get('intent'):
                    self.bot.journal.discard_proactive(task['id'], 'foreground_or_disabled')

    def step(self, now):
        try:
            self.priority(now)
            if not self.cfg['enabled'] or self.foreground():
                return
            if self.job:
                worker, through, expires = self.job
                result = worker.poll()
                if result is None:
                    return
                worker.close()
                self.job = None
                if not result.get('ok'):
                    raise ValueError('proactive_worker_failed')
                decision = parse_decision(json.dumps(result['decision'], ensure_ascii=False), self.cfg['max_reply_chars'], self.bot.cfg)
                if not decision['speak']:
                    self.update(outcome='silent', error='')
                    return
                if now > expires:
                    self.update(outcome='expired', error='')
                    return
                task = self.bot.journal.enqueue_proactive(decision['text'], through, now, expires)
                if task:
                    self.validate(task, now)
                    self.update(outcome='queued' if self.bot.journal.task(task['id'])['phase'] != 'done' else 'activity_changed', error='')
                return
            if self.bot.journal.tasks() or self.bot.ui_job or self.bot.echo:
                return
            limit = int(self.bot.cfg['queue']['generation_concurrency'])
            if len(self.bot.generators)+int(self.bot.memory_job is not None) >= limit:
                return
            state = self.state()
            if (self.bot.last_local_id <= state['last_checked_id']
                    or now-state['last_checked_at'] < self.cfg['check_interval_seconds']
                    or now-state['last_evaluated_at'] < self.cfg['evaluation_interval_seconds']
                    or now-self.bot.journal.get('last_success', 0) < self.cfg['window_seconds']):
                return
            rows, own = self.context(now)
            self.update(last_checked_at=now, last_checked_id=self.bot.last_local_id, message_count=len(rows))
            if not self.eligible(rows, own, now):
                self.update(outcome='below_threshold_or_bot_spoke')
                return
            new_count = sum(m['local_id'] > state['last_evaluated_id'] for m in rows)
            if new_count < (self.cfg['min_new_messages'] if state['evaluations'] else 1):
                return
            memory = None
            if self.bot.memory:
                labels = member_labels(rows, [rows[-1]], self.bot.store.self_wxid)
                memory = dict(labels=labels, text=self.bot.memory.prompt(labels, rows[-1]['sender'], now,
                    self.bot.cfg['memory']['prompt_max_chars'], use_avatar=self.bot.cfg['memory']['avatar']['enabled'],
                    dialogue='\n'.join(m.get('text', '') for m in rows),
                    use_learning=self.bot.cfg['memory']['learning']['enabled']))
            prompt = build_prompt(self.bot.cfg, rows, self.bot.chat_title, memory)
            through = max(m['local_id'] for m in rows)
            self.update(last_evaluated_id=through, last_evaluated_at=now,
                        evaluations=state['evaluations']+1, outcome='evaluating', error='')
            worker = self.bot.job_factory('proactive', (self.bot.cfg, prompt), timeout=self.cfg['timeout_seconds']+2)
            self.job = (worker, through, now+self.cfg['reply_ttl_seconds'])
            log.info('主动接话判断 messages=%s', len(rows))
        except Exception as exc:
            self.cancel('error')
            self.update(outcome='error', error=type(exc).__name__)
            log.warning('主动接话跳过 category=%s', type(exc).__name__)
