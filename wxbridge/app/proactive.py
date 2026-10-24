"""低优先级入场与短会话；数据库仍由主进程查询，发送复用确认队列。"""
from __future__ import annotations

import json
import logging

from wxbridge.ai.proactive import activity, addresses_bot, build_prompt, parse_decision, timeline, ProactiveError
from wxbridge.ai.memory import member_labels
from wxbridge.wechat.pats import is_pat
from wxbridge.app.conversation import SESSION_POLICY, observation_updates
from wxbridge.app.task_policy import proactive_failure_code
from wxbridge.storage.memory_selection import memory_dialogue

log = logging.getLogger('wxbridge')


class ProactiveController:
    def __init__(self, bot):
        self.bot, self.cfg, self.job = bot, bot.cfg['proactive'], None
        self.pending, self.dialogue = None, []
        self.latest_own_id = 0
        if bot.journal.get('proactive') is None:
            with bot.journal.db:
                bot.journal.set('proactive', dict(last_evaluated_id=bot.last_local_id,
                    evaluations=0, last_evaluated_at=0, last_checked_at=0,
                    last_checked_id=bot.last_local_id, outcome='waiting_new_messages', error=''))
        self.update(enabled=self.cfg['enabled'])
        session = bot.journal.get('proactive_session')
        if session and session.get('policy') != SESSION_POLICY:
            # 旧计时由机器人回复续期，无法当作成员活动证据；只迁移会话，不改收发任务。
            self.close_session('policy_updated', 0)
        with bot.journal.db:
            bot.journal.set('proactive_entry_not_before', 0)  # 清除旧版无人回应的额外十分钟退让。
        if self.state().get('outcome') in ('evaluating', 'generating_reply'):
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

    def fail(self, result):
        self.cancel('error')
        code = proactive_failure_code(result)
        chars = result.get('reply_chars')
        self.update(outcome='error', error=code,
                    reply_chars=chars if type(chars) is int and 0 <= chars <= 1000000 else None)
        log.warning('主动接话跳过 category=%s', code)

    def foreground(self):
        return any(t.get('trigger_kind') != 'proactive' for t in self.bot.journal.tasks())

    def session(self, now):
        value = self.bot.journal.get('proactive_session')
        if not value:
            return None
        limit = self.cfg['conversation']['max_followups']
        if (not self.cfg['conversation']['enabled'] or now >= value['expires_at']
                or (limit and value['followups'] >= limit)):
            self.close_session('expired' if now >= value['expires_at'] else 'finished', now)
            return None
        return value

    def close_session(self, reason, now):
        value = self.bot.journal.get('proactive_session')
        if not value:
            return
        with self.bot.journal.db:
            self.bot.journal.set('proactive_session', None)
            self.bot.journal.set('proactive_entry_floor', max(self.bot.last_local_id,
                self.bot.journal.get('proactive_entry_floor', 0)))

    def observe(self, message, accepted, now):
        """仅在 Journal.intake 的接收事务内调用，不另开事务或查询微信数据库。"""
        changes = observation_updates(message, accepted, now, self.bot.journal.get('proactive_session'),
            settings=self.cfg, account=self.bot.store.self_wxid)
        for key, value in changes.items():
            if key == 'proactive_entry_floor':
                value = max(value, self.bot.journal.get(key, 0))
            self.bot.journal.set(key, value)

    def context(self, now):
        raw = self.bot.store.messages_in_window(self.bot.chat_id, int(now-self.cfg['window_seconds']), int(now))
        raw = [m for m in raw if m['local_id'] <= self.bot.last_local_id]
        ordered = timeline(raw, now, self.cfg['window_seconds'])
        self.latest_own_id = max((m['local_id'] for m in ordered
            if m.get('sender') == self.bot.store.self_wxid and not is_pat(m)), default=0)
        self.dialogue = [m for m in ordered
                         if m.get('sender') and m.get('type') == 1 and (m.get('text') or '').strip()]
        return activity(raw, self.bot.store.self_wxid, now, self.cfg['window_seconds'])

    def eligible(self, rows, now):
        return (len(rows) >= self.cfg['min_messages']
                and now-rows[-1]['time'] <= self.cfg['reply_ttl_seconds'])

    def entry_block_reason(self, now):
        entries = self.bot.journal.get('proactive_entries', [])
        limit = self.cfg['max_entries_per_hour']
        if limit and len([t for t in entries if 0 <= now-t < 3600]) >= limit:
            return 'entry_budget'
        if entries and now-max(entries) < self.cfg['entry_cooldown_seconds']:
            return 'entry_cooldown'
        return ''

    def candidate(self, rows, now):
        state = self.state()
        # 已发言前的旧提问仍用于理解语境，不重新作为主动回复目标。
        session = self.session(now)
        floor = max(state['last_evaluated_id'], self.latest_own_id,
                    self.bot.journal.get('proactive_entry_floor', 0), session['start_id'] if session else 0)
        fresh = [m for m in rows if m['local_id'] > floor
                 and now-m['time'] <= self.cfg['reply_ttl_seconds']]
        if not fresh:
            self.update(outcome='waiting_new_messages')
            return None
        account = self.bot.store.self_wxid
        available = [m for m in fresh if not (m.get('mentioned_users') and account not in m['mentioned_users'])]
        opportunities = self.cfg['opportunities']
        named = [m for m in available if addresses_bot(m, self.bot.cfg, account)] if opportunities['enabled'] else []
        if session:
            if self.latest_own_id > max(session['anchor_id'], self.bot.journal.get('last_confirmed_outbound_id', 0)):
                self.close_session('manual_own_message', now)
                self.update(outcome='manual_own_message')
                return None
            replies = [m for m in fresh if m['local_id'] > session['anchor_id']
                       and not (m.get('mentioned_users') and account not in m['mentioned_users'])
                       and (not (m.get('reference') or {}).get('sender') or m['reference']['sender'] == account
                            or m in named)]
            if replies:
                return dict(mode='followup', reason='addressed' if named else 'conversation',
                            source_ids=[m['local_id'] for m in named or replies],
                            anchor_id=session['anchor_id'], session_id=session['session_id'],
                            expires_at=min(session['expires_at'], now+self.cfg['reply_ttl_seconds']))
            self.update(outcome='waiting_conversation_reply')
            return None
        if named:
            # 新消息点名/引用机器人是受邀接话，不受无人邀请入场的五分钟/六十秒限制。
            return dict(mode='entry', reason='addressed', source_ids=[m['local_id'] for m in named],
                        expires_at=now+self.cfg['reply_ttl_seconds'])
        blocked = self.entry_block_reason(now)
        if blocked:
            self.update(outcome=blocked)
            return None
        if now-state['last_evaluated_at'] < self.cfg['evaluation_interval_seconds']:
            self.update(outcome='evaluation_cooldown')
            return None
        if not available:
            self.update(outcome='addressed_to_others')
            return None
        enough_new = len(fresh) >= (self.cfg['min_new_messages'] if state['evaluations'] else 1)
        reason = 'activity' if enough_new and self.eligible(rows, now) else ''
        if (not reason and opportunities['enabled'] and len(fresh) >= self.cfg['min_new_messages']
                and len(rows) >= opportunities['min_messages']
                and len({m['sender'] for m in rows}) >= opportunities['min_senders']):
            reason = 'small_conversation'
        if not reason:
            self.update(outcome='below_threshold')
            return None
        return dict(mode='entry', reason=reason, source_ids=[m['local_id'] for m in available],
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
            rows, _ = self.context(now)
            newer = [m for m in rows if m['local_id'] > data['snapshot_id']]
            if self.latest_own_id > data['snapshot_id']:
                self.close_session('manual_own_message', now)
                reason = 'activity_changed'
            elif data.get('mode') == 'followup':
                session = self.session(now)
                if (not session or session['anchor_id'] != data['anchor_id']
                        or session['session_id'] != data.get('session_id')):
                    reason = 'conversation_finished'
                elif newer:
                    reason = 'context_changed'
            elif data.get('mode') == 'entry':
                if not rows:
                    reason = 'activity_changed'
                elif len(newer) >= self.cfg['min_new_messages']:
                    reason = 'context_changed'
                elif data.get('reason') == 'addressed':
                    if (not self.cfg['opportunities']['enabled'] or not any(
                            m['local_id'] in data['source_ids'] and addresses_bot(m, self.bot.cfg, self.bot.store.self_wxid)
                            for m in rows)):
                        reason = 'source_unavailable'
                else:
                    reason = self.entry_block_reason(now)
            elif not self.eligible(rows, now):
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
            if not self.cfg['enabled']:
                self.close_session('disabled', now)
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
                if result.get('event') == 'proactive_reply_started':
                    self.bot.metrics.record_models(result.get('model_metrics'))
                    self.update(outcome='generating_reply', model_role='reply', error='')
                    return
                worker.close()
                self.job = None
                self.bot.metrics.record_models(result.get('model_metrics'))
                pending, self.pending = self.pending, None
                if not result.get('ok'):
                    self.fail(result)
                    return
                decision = parse_decision(json.dumps(result['decision'], ensure_ascii=False), self.cfg['max_reply_chars'], self.bot.cfg)
                if not decision['speak']:
                    self.update(outcome='silent', error='')
                    return
                if now > expires:
                    self.update(outcome='expired', error='')
                    return
                target = decision.get('reply_to')
                if target is None and not (self.cfg['opportunities']['enabled'] or self.cfg['conversation']['enabled']):
                    target = pending['source_ids'][-1]  # 兼容旧协议的受控测试/关闭新功能的运行。
                if target not in pending['source_ids']:
                    raise ProactiveError('proactive_invalid_reply_target')
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
            rows, _ = self.context(now)
            self.update(last_checked_at=now, last_checked_id=self.bot.last_local_id, message_count=len(rows))
            candidate = self.candidate(rows, now)
            if not candidate:
                return
            labels = member_labels(self.dialogue, [], self.bot.store.self_wxid)
            prompt = build_prompt(self.bot.cfg, self.dialogue, self.bot.chat_title, dict(labels=labels),
                opportunity={k:v for k,v in candidate.items() if k != 'expires_at'}, account=self.bot.store.self_wxid)
            reply_memory = {}
            if self.bot.memory:
                cached = {}
                for source in rows:
                    if source['local_id'] not in candidate['source_ids']:
                        continue
                    history = [m for m in self.dialogue if m['local_id'] <= source['local_id']]
                    topic = memory_dialogue(history, [source], self.bot.store.self_wxid)
                    key = (source['sender'], topic)
                    if key not in cached:
                        cached[key] = self.bot.memory.prompt(labels, source['sender'], now,
                            self.bot.cfg['memory']['prompt_max_chars'], use_avatar=self.bot.cfg['memory']['avatar']['enabled'],
                            dialogue=topic, question=source.get('text', ''),
                            use_learning=self.bot.cfg['memory']['learning']['enabled'])
                    reply_memory[str(source['local_id'])] = cached[key]
            through = max(m['local_id'] for m in self.dialogue)
            self.update(last_evaluated_id=through, last_evaluated_at=now,
                        evaluations=state['evaluations']+1, outcome='evaluating', mode=candidate['mode'],
                        opportunity=candidate['reason'], model_role='participation', error='', reply_chars=None)
            from wxbridge.ai.roles import model_settings
            remaining = max(0, candidate['expires_at']-now)
            options = dict(source_ids=candidate['source_ids'], timeout_seconds=remaining, reply_memory=reply_memory)
            timeout = min(remaining, model_settings(self.bot.cfg, 'participation')['timeout_seconds']
                          + model_settings(self.bot.cfg, 'reply')['timeout_seconds'])+2
            worker = self.bot.job_factory('proactive', (self.bot.cfg, prompt, options), timeout=timeout)
            self.pending = candidate
            self.job = (worker, through, candidate['expires_at'])
            log.info('主动接话判断 mode=%s reason=%s messages=%s', candidate['mode'], candidate['reason'], len(rows))
        except Exception as exc:
            self.fail(dict(error=getattr(exc, 'code', type(exc).__name__),
                           reply_chars=exc.reply_chars if isinstance(exc, ProactiveError) else None))
