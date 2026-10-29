"""Small owner-written JSON profiles; model proposals cannot edit member identity."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


def _uid(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(c.isspace() or ord(c) < 32 for c in value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('profile_duplicate_key')
        result[key] = value
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class GroupProfile:
    def __init__(self, path, identity, *, summary_max_chars=1200):
        self.path = Path(path)
        self.identity = dict(identity)
        if (set(self.identity) != {'account', 'chat'}
                or not all(_uid(v) for v in self.identity.values())):
            raise ValueError('profile_invalid_identity')
        self.limit = summary_max_chars
        self._disk_hash = None
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('profile_unsafe_path')
        if self.path.exists():
            info = self.path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024
                    or (os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError('profile_unsafe_file')
            raw = self.path.read_bytes()
            self.data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_object)
            self._disk_hash = hashlib.sha256(raw).hexdigest()
            self._validate(self.data)
        else:
            self.data = dict(account_id=identity['account'], chat_id=identity['chat'],
                             analyzed_through=0, summary='', members={})
            self._commit(self.data)

    def _validate(self, value):
        if (not isinstance(value, dict)
                or set(value) != {'account_id', 'chat_id', 'analyzed_through', 'summary', 'members'}
                or value['account_id'] != self.identity['account'] or value['chat_id'] != self.identity['chat']):
            raise ValueError('profile_identity_mismatch')
        if (type(value['analyzed_through']) is not int or value['analyzed_through'] < 0
                or not isinstance(value['summary'], str) or len(value['summary']) > self.limit
                or not isinstance(value['members'], dict) or len(value['members']) > 10000):
            raise ValueError('profile_invalid_data')
        for uid, item in value['members'].items():
            if (not _uid(uid) or uid.endswith('@chatroom') or uid in ('notify@all', '@all')
                    or not isinstance(item, dict) or 'alias' not in item
                    or set(item) - {'alias', 'memory'} or not isinstance(item['alias'], str)
                    or len(item['alias']) > 256 or any(ord(c) < 32 for c in item['alias'])):
                raise ValueError('profile_invalid_member')
            if 'memory' in item and not isinstance(item['memory'], (str, list, dict)):
                raise ValueError('profile_invalid_memory')

    def _commit(self, value):
        self._validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('profile_unsafe_path')
        if self.path.exists():
            if hashlib.sha256(self.path.read_bytes()).hexdigest() != self._disk_hash:
                raise ValueError('profile_changed_on_disk')
        elif self._disk_hash is not None:
            raise ValueError('profile_changed_on_disk')
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('profile_size_limit')
        descriptor, temporary = tempfile.mkstemp(prefix='.profile-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.data, self._disk_hash = deepcopy(value), hashlib.sha256(raw).hexdigest()

    @property
    def analyzed_through(self):
        return self.data['analyzed_through']

    def observe(self, members):
        value = deepcopy(self.data)
        for uid, alias in members.items():
            if not _uid(uid) or not isinstance(alias, str):
                raise ValueError('profile_invalid_member')
            value['members'].setdefault(uid, dict(alias=''))['alias'] = alias
        if value != self.data:
            self._commit(value)

    def sync_memory(self, memories):
        value = deepcopy(self.data)
        for uid, memory in memories.items():
            if uid in value['members']:
                if memory:
                    value['members'][uid]['memory'] = deepcopy(memory)
                else:
                    value['members'][uid].pop('memory', None)
        if value != self.data:
            self._commit(value)

    def snapshot(self, *, members=None, memory_enabled=False):
        selected = self.data['members'] if members is None else {
            uid: self.data['members'][uid] for uid in members if uid in self.data['members']}
        value = dict(summary=self.data['summary'], members=deepcopy(selected))
        if not memory_enabled:
            for item in value['members'].values():
                item.pop('memory', None)
        return value

    def _token(self):
        return _digest(dict(identity=self.identity, summary=self.data['summary'],
                            analyzed_through=self.analyzed_through))

    def analysis_snapshot(self, messages, *, scan_through=None):
        messages = deepcopy(messages)
        through = max((m.get('local_id', 0) for m in messages), default=0)
        if scan_through is not None:
            through = scan_through
        return dict(identity=deepcopy(self.identity), token=self._token(),
                    profile=self.snapshot(memory_enabled=False), messages=messages,
                    source_ids=[m['id'] for m in messages], through=through)

    def apply(self, proposal, snapshot):
        if snapshot['identity'] != self.identity or snapshot['token'] != self._token():
            return False
        if (not isinstance(proposal, dict) or set(proposal) != {'summary', 'sources'}
                or not isinstance(proposal['summary'], str) or len(proposal['summary']) > self.limit
                or not isinstance(proposal['sources'], list)):
            raise ValueError('profile_invalid_proposal')
        allowed = {(type(v), v) for v in snapshot['source_ids']}
        sources = proposal['sources']
        if (any(type(v) not in (str, int) for v in sources)
                or any((type(v), v) not in allowed for v in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError('profile_unknown_source')
        if proposal['summary'] != self.data['summary'] and not sources:
            raise ValueError('profile_missing_sources')
        through = snapshot['through']
        if type(through) is not int or through < self.analyzed_through:
            raise ValueError('profile_invalid_watermark')
        value = deepcopy(self.data)
        value.update(summary=proposal['summary'], analyzed_through=through)
        if value != self.data:
            self._commit(value)
        return True
