"""Group profile lifecycle; only the session owner reads WeChat or writes JSON."""
from __future__ import annotations

import json
from pathlib import Path
import time

from wxbridge.ai.roles import model_settings
from wxbridge.storage.group_profile import GroupProfile
from wxbridge.storage.memory_rules import usable_entries


def participant_ids(rows, account):
    result = {account}
    for row in rows:
        if row.get('sender'):
            result.add(row['sender'])
        result.update(row.get('mentioned_users') or [])
        if row.get('pat'):
            result.add(row['pat']['target'])
        reference = row.get('reference')
        for _ in range(8):
            if not isinstance(reference, dict):
                break
            if reference.get('sender'):
                result.add(reference['sender'])
            reference = reference.get('reference')
    return {uid for uid in result if isinstance(uid, str) and uid
            and not uid.endswith('@chatroom') and uid not in ('notify@all', '@all')}


class ProfileController:
    def __init__(self, session):
        self.session, self.settings = session, session.cfg['profile']
        filename = session.cfg.get('_instance_config_file')
        if not filename:
            raise ValueError('profile_requires_instance_config')
        self.document = GroupProfile(Path(filename).parent/'profile.json',
                                     session.journal.get('identity'),
                                     summary_max_chars=self.settings['summary_max_chars'])
        self.job, self.pending = None, None
        self.error, self.next_at, self.aliases_at, self.bootstrap_end = '', 0, 0, 0
        self.observe([])

    def observe(self, rows):
        observed = participant_ids(rows, self.session.store.self_wxid)
        current = self.document.data['members']
        updates = {uid: '' for uid in observed if uid not in current}
        now = time.time()
        if updates or now >= self.aliases_at:
            self.aliases_at = now + 60
            contacts = self.session.store.contacts()
            if self.session.chat_id.endswith('@chatroom'):
                aliases = self.session.store.group_member_aliases(self.session.chat_id)
                for uid in observed | set(current) | set(aliases):
                    # An unavailable member snapshot is not evidence that an old card was cleared.
                    alias = aliases[uid] if uid in aliases else current.get(uid, {}).get('alias', '')
                    updates[uid] = alias or contacts.get(uid, {}).get('nick_name') or ''
            else:
                # Private chats have no group cards, so contact nicknames provide the alias.
                for uid in observed | set(current):
                    updates[uid] = contacts.get(uid, {}).get('nick_name') or ''
        self.document.observe(updates)

    def snapshot(self, rows):
        self.observe(rows)
        members = participant_ids(rows, self.session.store.self_wxid)
        # A group summary can name somebody absent from this short message window.
        members.update(uid for uid in self.document.data['members'] if uid in self.document.data['summary'])
        memory = self.session.memory
        if memory:
            now, view = time.time(), memory.view()
            summaries = {uid: [dict(kind=e['kind'], summary=e['summary'], time=e['occurred_at'])
                               for e in usable_entries(person, now)[-4:]]
                         for uid, person in view.members.items() if uid in members}
            self.document.sync_memory(summaries)
        return self.document.snapshot(members=members, memory_enabled=memory is not None)

    def _collect(self):
        if self.job is None:
            return
        result = self.job.poll()
        if result is None:
            return
        self.job.close()
        self.job, pending = None, self.pending
        self.pending = None
        self.session.metrics.record_models(result.get('model_metrics'))
        if not result.get('ok'):
            self.error = result.get('error', 'profile_analysis_failed')
            self.next_at = time.time() + 60
            return
        if not self.document.apply(result['proposal'], pending):
            self.error = 'profile_stale_result'
            self.next_at = time.time() + 60
            return
        self.error = ''
        self.next_at = (0 if self.document.analyzed_through < self.bootstrap_end
                        else time.time() + self.settings['min_interval_seconds'])

    def _rows(self):
        bot = self.session
        if self.document.analyzed_through == 0:
            cutoff = time.time() - self.settings['initial_history_days'] * 86400
            rows = [m for m in bot.store.recent_messages(bot.chat_id, self.settings['initial_max_messages'])
                    if m.get('time', 0) >= cutoff and m['local_id'] <= bot.last_local_id]
            self.bootstrap_end = max((m['local_id'] for m in rows), default=bot.last_local_id)
        else:
            rows = bot.store.messages_after(bot.chat_id, self.document.analyzed_through,
                                            limit=self.settings['batch_size'])
            rows = [m for m in rows if m['local_id'] <= bot.last_local_id]
        rows.sort(key=lambda m: m['local_id'])
        return rows[:self.settings['batch_size']]

    def step(self, *, collect_only=False):
        try:
            self._collect()
            if collect_only or self.job is not None or time.time() < self.next_at:
                return
            background = self.session.models.status()['background']
            if background['running'] or background['queued'] or self.session.has_foreground_tasks():
                return
            rows = self._rows()
            initial = self.document.analyzed_through < self.bootstrap_end or self.document.analyzed_through == 0
            if not rows:
                self.next_at = time.time() + 60
                return
            if not initial and len(rows) < self.settings['min_new_messages']:
                self.next_at = time.time() + 60
                return
            self.observe(rows)
            messages, through = [], self.document.analyzed_through
            for row in rows:
                if row.get('type') == 1 and row.get('sender') and (row.get('text') or '').strip():
                    message = dict(id='m'+str(row['local_id']), local_id=row['local_id'],
                                   sender=row['sender'], time=row['time'], text=row['text'],
                                   is_bot=row['sender'] == self.session.store.self_wxid)
                    if isinstance(row.get('reference'), dict):
                        message['reference'] = row['reference']
                    candidate = messages + [message]
                    profile = self.document.snapshot(members=participant_ids(rows, self.session.store.self_wxid))
                    size = len(json.dumps(dict(chat=self.session.chat_title, bot=self.session.store.self_wxid,
                                               **profile, messages=candidate), ensure_ascii=False))
                    if size > self.settings['analysis_max_chars']:
                        if messages:
                            break  # Keep this whole message for the next batch.
                        # An oversized individual message is not truncated into false evidence.
                        through = row['local_id']
                        continue
                    messages.append(message)
                through = row['local_id']
            pending = self.document.analysis_snapshot(messages, scan_through=through)
            pending['profile'] = self.document.snapshot(members=participant_ids(rows, self.session.store.self_wxid))
            if not messages:
                self.document.apply(dict(summary=pending['profile']['summary'], sources=[]), pending)
                self.next_at = time.time() + 60
                return
            payload = dict(chat=self.session.chat_title, bot=self.session.store.self_wxid,
                           **pending['profile'], messages=messages)
            self.job = self.session.job_factory('group_profile', (self.session.cfg, payload),
                timeout=model_settings(self.session.cfg, 'group_profile')['timeout_seconds'] + 2)
            self.pending = pending
            self.error = ''
        except Exception as error:
            self.error = str(error) if isinstance(error, ValueError) and str(error).startswith('profile_') else type(error).__name__
            self.next_at = time.time() + 60

    def status(self):
        return dict(enabled=True, running=self.job is not None, members=len(self.document.data['members']),
                    analyzed_through=self.document.analyzed_through,
                    ready=bool(self.document.data['summary']), error=self.error)

    def close(self):
        if self.job:
            self.job.close()
            self.job, self.pending = None, None
