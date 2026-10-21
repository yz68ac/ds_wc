"""低优先级入场与短会话；数据库仍由主进程查询，发送复用确认队列。"""
from __future__ import annotations

import json
import logging

from wxbridge.ai.proactive import activity, addresses_bot, build_prompt, parse_decision, timeline
from wxbridge.ai.memory import member_labels

log = logging.getLogger('wxbridge')


class ProactiveController:
    def __init__(self, bot):
        self.bot, self.cfg, self.job = bot, bot.cfg['proactive'], None
        self.pending, self.dialogue = None, []
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
            self.pending = None
            self.update(outcome=reason)

    def foreground(self):
        return any(t.get('trigger_kind') != 'proactive' for t in self.bot.journal.tasks())

    def session(self, now):
        value = self.bot.journal.get('proactive_session')
        if not value:
            return None
        if (not self.cfg['conversation']['enabled'] or now >= value['expires_at']
                or value['followups'] >= self.cfg['conversation']['max_followups']):
            self.close_session('expired' if now >= value['expires_at'] else 'finished', now)
            return None
        return value

    def close_session(self, reason, now):
        value = self.bot.journal.get('proactive_session')
        if not value:
            return
        with self.bot.journal.db:
            if value and value['origin'] == 'entry' and not value['followups'] and reason == 'expired':
                self.bot.journal.set('proactive_entry_not_before', now+2*self.cfg['entry_cooldown_seconds'])
            self.bot.journal.set('proactive_session', None)

    def context(self, now):
        raw = self.bot.store.messages_in_window(self.bot.chat_id, int(now-self.cfg['window_seconds']), int(now))
        raw = [m for m in raw if m['local_id'] <= self.bot.last_local_id]
        self.dialogue = [m for m in timeline(raw, now, self.cfg['window_seconds'])
                         if m.get('sender') and m.get('type') == 1 and (m.get('text') or '').strip()]
        return activity(raw, self.bot.store.self_wxid, now, self.cfg['window_seconds'])

    def eligible(self, rows, own, now):
        return (not own and len(rows) >= self.cfg['min_messages']
                and now-rows[-1]['time'] <= self.cfg['reply_ttl_seconds'])

    def candidate(self, rows, own, now):
        state = self.state()
        fresh = [m for m in rows if m['local_id'] > state['last_evaluated_id']
                 and now-m['time'] <= self.cfg['reply_ttl_seconds']]
        if not fresh:
            return None
        account = self.bot.store.self_wxid
        session = self.session(now)
        if session:
            if any(m['sender'] == account and m['local_id'] > session['anchor_id'] for m in self.dialogue):
                self.close_session('manual_own_message', now)
                return None
            replies = [m for m in fresh if m['local_id'] > session['anchor_id']
                       and not (m.get('mentioned_users') and account not in m['mentioned_users'])
                       and (not (m.get('reference') or {}).get('sender') or m['reference']['sender'] == account)
                       and (m['sender'] == session['participant'] or addresses_bot(m, self.bot.cfg, account))]
            if replies:
                return dict(mode='followup', reason='conversation', source_ids=[m['local_id'] for m in replies],
                            anchor_id=session['anchor_id'], expires_at=min(session['expires_at'], now+self.cfg['reply_ttl_seconds']))
            if len(fresh) >= self.cfg['min_new_messages']:
                self.close_session('conversation_moved_on', now)
            return None
        if (own or now-self.bot.journal.get('last_success', 0) < self.cfg['entry_cooldown_seconds']
                or now < self.bot.journal.get('proactive_entry_not_before', 0)
                or now-state['last_evaluated_at'] < self.cfg['evaluation_interval_seconds']):
            return None
        entries = [t for t in self.bot.journal.get('proactive_entries', []) if 0 <= now-t < 3600]
        if len(entries) >= self.cfg['max_entries_per_hour']:
            self.update(outcome='entry_budget')
            return None
        available = [m for m in fresh if not (m.get('mentioned_users') and account not in m['mentioned_users'])]
        if not available:
            return None
        opportunities = self.cfg['opportunities']
        named = [m for m in available if addresses_bot(m, self.bot.cfg, account)] if opportunities['enabled'] else []
        enough_new = len(fresh) >= (self.cfg['min_new_messages'] if state['evaluations'] else 1)
        reason = 'addressed' if named else 'activity' if enough_new and self.eligible(rows, own, now) else ''
        if (not reason and opportunities['enabled'] and len(fresh) >= self.cfg['min_new_messages']
                and len(rows) >= opportunities['min_messages']
                and len({m['sender'] for m in rows}) >= opportunities['min_senders']):
            reason = 'small_conversation'
        if not reason:
            return None
        return dict(mode='entry', reason=reason, source_ids=[m['local_id'] for m in named or available],
                    expires_at=now+self.cfg['reply_ttl_seconds'])

    def _validate(self, task, now):
        """有发送意图只能核对；未提交的短句必须重验来源、会话和新消息。"""
        if task.get('trigger_kind') != 'proactive' or task.get('intent'):
            return True
        data = task['proactive']
        reason = ''
        if not self.cfg['enabled'] or self.foreground():
            reason = 'foreground_or_disabled'
        elif now > data['expires_at']:
            reason = 'expired'
        else:
            rows, own = self.context(now)
            newer = [m for m in rows if m['local_id'] > data['snapshot_id']]
            if data.get('mode') == 'followup':
                session = self.session(now)
                if not session or session['anchor_id'] != data['anchor_id']:
                    reason = 'conversation_finished'
                elif any(m['sender'] == self.bot.store.self_wxid and m['local_id'] > session['anchor_id'] for m in self.dialogue):
                    self.close_session('manual_own_message', now)
                    reason = 'activity_changed'
                elif newer:
                    reason = 'context_changed'
            elif data.get('mode') == 'entry':
                if own or not rows:
                    reason = 'activity_changed'
                elif len(newer) >= self.cfg['min_new_messages']:
                    reason = 'context_changed'
                elif len([t for t in self.bot.journal.get('proactive_entries', []) if 0 <= now-t < 3600]) >= self.cfg['max_entries_per_hour']:
                    reason = 'entry_budget'
            elif not self.eligible(rows, own, now):
                reason = 'activity_changed'  # 旧版已入队短句仍使用原门槛。
            elif len(newer) >= self.cfg['min_new_messages']:
                reason = 'context_changed'
            if not reason and data.get('reply_to') is not None:
                if not any(m['local_id'] == data['reply_to'] and m['sender'] == data['recipient'] for m in rows):
                    reason = 'source_unavailable'
        if reason:
            self.bot.journal.discard_proactive(task['id'], reason)
            self.update(outcome=reason)
            return False
        return True

    def validate(self, task, now):
        try:
            return self._validate(task, now)
        except Exception as exc:
            self.bot.journal.discard_proactive(task['id'], 'activity_unavailable')
            self.update(outcome='activity_unavailable', error=type(exc).__name__)
            return False

    def priority(self, now):
        if self.foreground() or not self.cfg['enabled']:
            self.cancel('foreground_or_disabled')
            self.close_session('foreground_or_disabled', now)
            for task in self.bot.journal.tasks():
                if task.get('trigger_kind') == 'proactive' and not task.get('intent'):
                    self.bot.journal.discard_proactive(task['id'], 'foreground_or_disabled')

    def step(self, now):
        try:
            self.priority(now)
            self.session(now)
            if not self.cfg['enabled'] or self.foreground():
                return
            if self.job:
                worker, through, expires = self.job
                result = worker.poll()
                if result is None:
                    return
                worker.close()
                self.job = None
                pending, self.pending = self.pending, None
                if not result.get('ok'):
                    raise ValueError('proactive_worker_failed')
                decision = parse_decision(json.dumps(result['decision'], ensure_ascii=False), self.cfg['max_reply_chars'], self.bot.cfg)
                if not decision['speak']:
                    if pending['mode'] == 'followup':
                        self.close_session('silent', now)
                    self.update(outcome='silent', error='')
                    return
                if now > expires:
                    self.update(outcome='expired', error='')
                    return
                target = decision.get('reply_to')
                if target is None and not (self.cfg['opportunities']['enabled'] or self.cfg['conversation']['enabled']):
                    target = pending['source_ids'][-1]  # 兼容旧协议的受控测试/关闭新功能的运行。
                if target not in pending['source_ids']:
                    raise ValueError('proactive_invalid_reply_target')
                rows, _ = self.context(now)
                source = next((m for m in rows if m['local_id'] == target), None)
                if source is None:
                    self.update(outcome='source_unavailable')
                    return
                metadata = {**pending, 'reply_to': target, 'recipient': source['sender']}
                task = self.bot.journal.enqueue_proactive(decision['text'], through, now, expires, metadata=metadata)
                if task:
                    self.validate(task, now)
                    self.update(outcome='queued' if self.bot.journal.task(task['id'])['phase'] != 'done' else self.state()['outcome'], error='')
                return
            if self.bot.journal.tasks() or self.bot.ui_job or self.bot.echo:
                return
            limit = int(self.bot.cfg['queue']['generation_concurrency'])
            if len(self.bot.generators)+int(self.bot.memory_job is not None) >= limit:
                return
            state = self.state()
            if (self.bot.last_local_id <= state['last_checked_id']
                    or now-state['last_checked_at'] < self.cfg['check_interval_seconds']):
                return
            rows, own = self.context(now)
            self.update(last_checked_at=now, last_checked_id=self.bot.last_local_id, message_count=len(rows))
            candidate = self.candidate(rows, own, now)
            if not candidate:
                if self.state().get('outcome') != 'entry_budget':
                    self.update(outcome='below_threshold_or_bot_spoke')
                return
            memory = None
            if self.bot.memory:
                labels = member_labels(rows, [rows[-1]], self.bot.store.self_wxid)
                memory = dict(labels=labels, text=self.bot.memory.prompt(labels, rows[-1]['sender'], now,
                    self.bot.cfg['memory']['prompt_max_chars'], use_avatar=self.bot.cfg['memory']['avatar']['enabled'],
                    dialogue='\n'.join(m.get('text', '') for m in self.dialogue),
                    use_learning=self.bot.cfg['memory']['learning']['enabled']))
            prompt = build_prompt(self.bot.cfg, self.dialogue, self.bot.chat_title, memory,
                opportunity={k:v for k,v in candidate.items() if k != 'expires_at'}, account=self.bot.store.self_wxid)
            through = max(m['local_id'] for m in self.dialogue)
            self.update(last_evaluated_id=through, last_evaluated_at=now,
                        evaluations=state['evaluations']+1, outcome='evaluating', mode=candidate['mode'],
                        opportunity=candidate['reason'], error='')
            worker = self.bot.job_factory('proactive', (self.bot.cfg, prompt), timeout=self.cfg['timeout_seconds']+2)
            self.pending = candidate
            self.job = (worker, through, candidate['expires_at'])
            log.info('主动接话判断 mode=%s reason=%s messages=%s', candidate['mode'], candidate['reason'], len(rows))
        except Exception as exc:
            self.cancel('error')
            self.update(outcome='error', error=type(exc).__name__)
            log.warning('主动接话跳过 category=%s', type(exc).__name__)
