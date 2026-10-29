"""Durable per-message participation decisions inside the existing queue database."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import uuid

ACTIVE = ('pending', 'running')
TERMINAL = ('approved', 'silent', 'expired', 'error', 'cancelled')


def _message_time(message):
    value = message.get('time')
    if message.get('time_source') in ('unknown', 'observed') or type(value) is not int or value <= 0:
        return None
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class ParticipationJournal:
    def _init_participation(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS participation_messages (
                local_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS participation_active
                ON participation_messages(state, local_id);
        """)
        self._participation_records(active=False)

    @staticmethod
    def _checked_participation(local_id, state, raw):
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError
            valid = (state in (*ACTIVE, *TERMINAL)
                and record.get('state') == state and type(record.get('local_id')) is int
                and record['local_id'] == local_id and local_id > 0
                and isinstance(record.get('sender'), str) and bool(record['sender'])
                and type(record.get('attempts')) is int and 0 <= record['attempts'] <= 2
                and _number(record.get('received_at')) and _number(record.get('expires_at'))
                and _number(record.get('retry_at')))
            if state in ACTIVE:
                message = record.get('message')
                valid = (valid and isinstance(message, dict) and type(message.get('local_id')) is int
                    and message['local_id'] == local_id and message.get('sender') == record['sender']
                    and _message_time(message) is not None and record['expires_at'] > message['time'])
                valid = valid and (bool(re.fullmatch(r'[a-f0-9]{32}', record.get('token', '')))
                    if state == 'running' else 'token' not in record)
            elif 'message' in record or 'token' in record:
                valid = False
            if not valid:
                raise ValueError
            return record
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ValueError('participation_invalid_queue_state') from None

    def _has_participation(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='participation_messages'").fetchone() is not None

    def _participation_records(self, *, active=True):
        clause = " WHERE state IN ('pending','running')" if active else ''
        return [self._checked_participation(*row) for row in self.db.execute(
            'SELECT local_id,state,data FROM participation_messages'+clause+' ORDER BY local_id')]

    def _put_participation(self, record):
        raw = json.dumps(record, ensure_ascii=False)
        self._checked_participation(record['local_id'], record['state'], raw)
        self.db.execute('INSERT OR REPLACE INTO participation_messages VALUES (?,?,?)',
                        (record['local_id'], record['state'], raw))

    def _finish_participation(self, record, state, now, *, code='', task_id=None):
        record.update(state=state, completed_at=now, error=code)
        record.pop('message', None)
        record.pop('token', None)
        if task_id is not None:
            record['task_id'] = task_id
        self._put_participation(record)
        counts = self.get('participation_counts', {})
        counts[state] = counts.get(state, 0)+1
        self.set('participation_counts', counts)

    def _expire_participation(self, now, *, include_running=False):
        for record in self._participation_records():
            if now >= record['expires_at'] and (record['state'] == 'pending' or include_running):
                self._finish_participation(record, 'expired', now, code='participation_expired')

    def _candidate(self, message, now, ttl_seconds):
        sent = _message_time(message)
        identifier, sender = message.get('local_id'), message.get('sender')
        identity = self.get('identity')
        if (not _number(now) or not _number(ttl_seconds) or ttl_seconds <= 0
                or type(identifier) is not int or identifier < 1 or sent is None or sent > now
                or not isinstance(sender, str) or not sender or sender == identity['account']
                or ('chat_id' in message and message['chat_id'] != identity['chat'])):
            raise ValueError('participation_invalid_candidate')
        return dict(local_id=identifier, sender=sender, received_at=now,
                    expires_at=sent+ttl_seconds, state='pending', attempts=0, retry_at=0,
                    message=deepcopy(message))

    def intake_participation(self, messages, accept, now, *, ttl_seconds, limits, describe=None):
        """Commit candidates and intake cursor together; capacity never drops unread input."""
        events = []
        with self.db:
            self._expire_participation(now)
            cursor = self.get('cursor', 0)
            active = self._participation_records()
            tasks = self.tasks()
            for message in sorted(messages, key=lambda m: m['local_id']):
                identifier = message['local_id']
                if type(identifier) is not int or identifier < 1:
                    raise ValueError('participation_invalid_candidate')
                if identifier <= cursor:
                    continue
                event = describe(message) if describe else None
                if accept(message):
                    total = limits.get('max_pending', 32)
                    per_sender = limits.get('max_pending_per_sender', 8)
                    count = len(active)+len(tasks)
                    sender_count = sum(r['sender'] == message['sender'] for r in active)
                    sender_count += sum(t.get('sender') == message['sender'] for t in tasks)
                    if ((total and count >= total) or (per_sender and sender_count >= per_sender)):
                        self.set('intake_deferred', dict(local_id=identifier, accepted_at=now))
                        self.set('backpressure', dict(reason='queue_capacity', at=now, local_id=identifier))
                        break
                    record = self._candidate(message, now, ttl_seconds)
                    # The cursor normally proves uniqueness. INSERT OR IGNORE also
                    # protects migrated sources whose IDs precede the cursor.
                    exists = self.db.execute('SELECT 1 FROM participation_messages WHERE local_id=?',
                                             (identifier,)).fetchone()
                    if not exists:
                        self._put_participation(record)
                        if now >= record['expires_at']:
                            self._finish_participation(record, 'expired', now, code='participation_expired')
                        else:
                            active.append(record)
                if event is not None:
                    events.append(event)
                cursor = max(cursor, identifier)
                deferred = self.get('intake_deferred') or {}
                if deferred.get('local_id', cursor+1) <= cursor:
                    self.set('intake_deferred', None)
                    self.set('backpressure', None)
            self.set('cursor', cursor)
            if events:
                self.set('recent_intake', (self.get('recent_intake', [])+events)[-50:])
        return events

    def reserve_participation(self, now, *, debounce_seconds, max_wait_seconds, max_batch_messages):
        """Reserve one immutable FIFO batch; new arrivals cannot invalidate its token."""
        if (not _number(now) or not _number(debounce_seconds) or debounce_seconds < 0
                or not _number(max_wait_seconds) or max_wait_seconds < debounce_seconds
                or type(max_batch_messages) is not int or max_batch_messages < 1):
            raise ValueError('participation_invalid_batch_options')
        with self.db:
            self._expire_participation(now)
            active = self._participation_records()
            if any(r['state'] == 'running' for r in active):
                return None
            ready = sorted((r for r in active if r['retry_at'] <= now),
                           key=lambda r: (r['received_at'], r['local_id']))
            if not ready:
                return None
            latest = max(r['received_at'] for r in ready)
            first = ready[0]['received_at']
            if (len(ready) < max_batch_messages and now < latest+debounce_seconds
                    and now < first+max_wait_seconds):
                return None
            selected, token = ready[:max_batch_messages], uuid.uuid4().hex
            for record in selected:
                record.update(state='running', token=token, attempts=record['attempts']+1,
                              started_at=now)
                self._put_participation(record)
            return dict(identity=deepcopy(self.get('identity')), token=token,
                        instance_id=self.get('instance_id'),
                        messages=[deepcopy(r['message']) for r in selected],
                        source_ids=[r['local_id'] for r in selected])

    def _reserved_participation(self, snapshot):
        if (not isinstance(snapshot, dict) or snapshot.get('identity') != self.get('identity')
                or snapshot.get('instance_id') != self.get('instance_id')
                or not isinstance(snapshot.get('token'), str) or not snapshot['token']
                or not isinstance(snapshot.get('source_ids'), list) or not snapshot['source_ids']
                or any(type(i) is not int or i < 1 for i in snapshot['source_ids'])
                or len(set(snapshot['source_ids'])) != len(snapshot['source_ids'])
                or not isinstance(snapshot.get('messages'), list)
                or any(not isinstance(m, dict) for m in snapshot.get('messages', []))
                or len(snapshot['messages']) != len(snapshot['source_ids'])):
            raise ValueError('participation_stale_result')
        records = []
        for identifier, message in zip(snapshot['source_ids'], snapshot['messages']):
            row = self.db.execute('SELECT local_id,state,data FROM participation_messages WHERE local_id=?',
                                  (identifier,)).fetchone()
            record = self._checked_participation(*row) if row else None
            if (not record or record['state'] != 'running' or record.get('token') != snapshot['token']
                    or record.get('message') != message or message.get('local_id') != identifier):
                raise ValueError('participation_stale_result')
            records.append(record)
        # A partial snapshot must not strand other members of the same reservation.
        reserved = {r['local_id'] for r in self._participation_records()
                    if r.get('token') == snapshot['token']}
        if reserved != set(snapshot['source_ids']):
            raise ValueError('participation_stale_result')
        if records != sorted(records, key=lambda r: (r['received_at'], r['local_id'])):
            raise ValueError('participation_stale_result')
        return records

    def release_participation(self, snapshot, now):
        """Undo an unused reservation without consuming a model-attempt budget."""
        with self.db:
            try:
                records = self._reserved_participation(snapshot)
            except ValueError:
                return False
            for record in records:
                if now >= record['expires_at']:
                    self._finish_participation(record, 'expired', now, code='participation_expired')
                else:
                    record.update(state='pending', attempts=max(0, record['attempts']-1))
                    record.pop('token', None)
                    self._put_participation(record)
            return True

    def apply_participation(self, snapshot, reply_to, now, *, reply_deadline=None):
        if (not isinstance(reply_to, list) or any(type(i) is not int or i < 1 for i in reply_to)
                or len(set(reply_to)) != len(reply_to)):
            raise ValueError('participation_invalid_reply_target')
        if reply_deadline is not None and (not _number(reply_deadline) or reply_deadline <= now):
            raise ValueError('participation_invalid_reply_deadline')
        with self.db:
            records = self._reserved_participation(snapshot)
            if set(reply_to)-set(snapshot['source_ids']):
                raise ValueError('participation_invalid_reply_target')
            selected, groups = set(reply_to), {}
            for record in records:
                if now >= record['expires_at']:
                    self._finish_participation(record, 'expired', now, code='participation_expired')
                elif record['local_id'] not in selected:
                    self._finish_participation(record, 'silent', now)
                else:
                    groups.setdefault(record['sender'], []).append(record)
            tasks = []
            for sender, sources in groups.items():
                task_id = self._next_task_id()
                task = dict(id=task_id, sender=sender, phase='queued', version=1,
                            trigger_kind='participation', triggers=[deepcopy(r['message']) for r in sources],
                            trigger_ids=[r['local_id'] for r in sources], generation='pending', chunks=[],
                            next_chunk=0, intent=None, blocked_reason='', deadline=now, closed_at=now,
                            accepted_at=min(r['received_at'] for r in sources), decision_at=now)
                if reply_deadline is not None:
                    task['reply_expires_at'] = reply_deadline
                self.put(task)
                for record in sources:
                    self._finish_participation(record, 'approved', now, task_id=task_id)
                tasks.append(task)
            self.set('participation_last_decision', dict(at=now, source_ids=snapshot['source_ids'],
                reply_to=[r['local_id'] for sources in groups.values() for r in sources],
                task_ids=[t['id'] for t in tasks]))
            return tasks

    def fail_participation(self, snapshot, now, code, *, retry=True):
        code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', code) else 'participation_failed'
        with self.db:
            try:
                records = self._reserved_participation(snapshot)
            except ValueError:
                return False
            for record in records:
                if now >= record['expires_at']:
                    self._finish_participation(record, 'expired', now, code='participation_expired')
                elif retry and record['attempts'] < 2 and now+2 < record['expires_at']:
                    record.update(state='pending', retry_at=now+2, error=code)
                    record.pop('token', None)
                    self._put_participation(record)
                else:
                    self._finish_participation(record, 'error', now, code=code)
            self.set('participation_last_error', dict(at=now, code=code))
            return True

    def recover_participation(self, now):
        with self.db:
            self._expire_participation(now, include_running=True)
            for record in self._participation_records():
                if record['state'] == 'running':
                    # Process death invalidates the reservation, not the original
                    # source age. Recovery does not grant another retry budget.
                    if record['attempts'] >= 2:
                        self._finish_participation(record, 'error', now, code='participation_interrupted')
                    else:
                        record.update(state='pending', retry_at=now, error='participation_interrupted')
                        record.pop('token', None)
                        self._put_participation(record)

    def cancel_participation(self, now, code='disabled'):
        with self.db:
            for record in self._participation_records():
                self._finish_participation(record, 'cancelled', now, code=code)

    def migrate_participation(self, now, ttl_seconds):
        """One-time conversion of unsent legacy automatic work; send intents stay intact."""
        if self.get('participation_schema') == 1:
            return
        with self.db:
            for task in self.tasks():
                kind = task.get('trigger_kind', 'mention')
                if (task.get('intent') is not None or task.get('next_chunk', 0) > 0
                        or task.get('last_notice_ack_id') is not None
                        or (task.get('notice') or {}).get('state') == 'confirmed' or kind == 'external'
                        or task.get('external_request_id') or kind == 'participation'):
                    continue
                if kind not in ('mention', 'addressed', 'pat', 'proactive'):
                    continue
                bound = 0
                for message in task.get('triggers', []):
                    try:
                        sent = _message_time(message)
                        if sent is None or sent > now:
                            continue
                        received_at = task.get('accepted_at', now)
                        if not _number(received_at) or received_at > now:
                            received_at = now
                        # A merged legacy task records the first receipt only.
                        # Later sources must keep their own original age.
                        record = self._candidate(message, max(received_at, sent), ttl_seconds)
                    except (ValueError, TypeError):
                        continue
                    if self.db.execute('SELECT 1 FROM participation_messages WHERE local_id=?',
                                       (record['local_id'],)).fetchone():
                        continue
                    self._put_participation(record)
                    if now >= record['expires_at']:
                        self._finish_participation(record, 'expired', now, code='participation_expired')
                    bound += 1
                task.update(phase='done', triggers=[], chunks=[], completed_at=now,
                            cancelled='participation_migrated' if bound else 'participation_unbound_legacy',
                            generation='silent', blocked_reason='')
                task.pop('notice', None)
                self.put(task)
            for key in ('proactive', 'proactive_session', 'proactive_entries', 'proactive_entry_not_before',
                        'proactive_entry_floor', 'addressed_through'):
                self.db.execute('DELETE FROM meta WHERE key=?', (key,))
            self.set('participation_schema', 1)

    def prune_participation(self, keep):
        if self._has_participation():
            self.db.execute("DELETE FROM participation_messages WHERE state NOT IN ('pending','running') "
                'AND local_id NOT IN (SELECT local_id FROM participation_messages '
                "WHERE state NOT IN ('pending','running') ORDER BY local_id DESC LIMIT ?)", (keep,))

    def participation_status(self):
        if not self._has_participation():
            return dict(outcome='not_started', pending=0, running=0, counts={}, recent=[])
        active = self._participation_records()
        pending = sum(r['state'] == 'pending' for r in active)
        running = sum(r['state'] == 'running' for r in active)
        recent = [self._checked_participation(*row) for row in self.db.execute(
            'SELECT local_id,state,data FROM participation_messages ORDER BY local_id DESC LIMIT 20')]
        metadata = self.get('participation', {})
        states = {state: 0 for state in (*ACTIVE, *TERMINAL)}
        for state, count in self.db.execute('SELECT state,COUNT(*) FROM participation_messages GROUP BY state'):
            states[state] = count
        return dict(dict({'outcome': 'evaluating' if running else 'waiting' if pending else 'idle'}, **metadata),
                    pending=pending, running=running, counts=self.get('participation_counts', {}),
                    state_counts=states,
                    last_decision=metadata.get('last_decision', self.get('participation_last_decision')),
                    last_error=self.get('participation_last_error'),
                    recent=[{k: r[k] for k in ('local_id', 'state', 'attempts', 'received_at',
                              'expires_at', 'completed_at', 'error', 'task_id') if k in r} for r in recent])
