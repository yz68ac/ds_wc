"""Versioned, read-only queue metadata for recovery and release tooling.

This module must not import Journal: inspecting an old queue never migrates it.
Only identifiers and counts leave SQLite; task and memory bodies stay there.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from wxbridge.storage.task_state import validate_task_states


@dataclass(frozen=True)
class QueueInspection:
    schema: int
    storage_schema: str
    identity: dict[str, str]
    cursor: int
    pending: int
    memory_pending: int
    memory_total: int
    has_intent: bool
    external_requests: int
    instance_id: str | None = None

    def release_summary(self):
        return dict(identity=self.identity, cursor=self.cursor, pending=self.pending,
                    memory_pending=self.memory_pending)


def inspect_queue(path) -> QueueInspection:
    target = Path(path).absolute()
    if target.is_symlink() or not target.is_file():
        raise ValueError('queue_inspection_invalid_path')
    with closing(sqlite3.connect(target.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != 0:
            raise ValueError('queue_inspection_unsupported_schema')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'meta', 'tasks'} <= tables:
            raise ValueError('queue_inspection_unsupported_schema')
        validate_task_states(db)
        meta = {k: json.loads(v) for k, v in db.execute(
            "SELECT key,value FROM meta WHERE key IN ('identity','cursor','instance_id')")}
        identity, cursor = meta.get('identity'), meta.get('cursor')
        if (not isinstance(identity, dict) or set(identity) != {'account', 'chat'}
                or any(not isinstance(v, str) or not v for v in identity.values())
                or type(cursor) is not int or cursor < 0):
            raise ValueError('queue_inspection_invalid_metadata')
        instance_id = meta.get('instance_id')
        if instance_id is not None and (not isinstance(instance_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', instance_id)):
            raise ValueError('queue_inspection_invalid_instance')
        columns = {r[1] for r in db.execute('PRAGMA table_xinfo(tasks)')}
        indexed = {'phase', 'has_intent', 'deadline', 'optional'} <= columns
        phase = 'phase' if indexed else "json_extract(data,'$.phase')"
        intent = 'has_intent=1' if indexed else "json_type(data,'$.intent')='object'"
        pending = db.execute(f"SELECT COUNT(*) FROM tasks WHERE {phase} != 'done'").fetchone()[0]
        has_intent = db.execute(f"SELECT 1 FROM tasks WHERE {phase} != 'done' AND {intent} LIMIT 1").fetchone() is not None
        memory_total = memory_pending = 0
        if 'memory_jobs' in tables:
            memory_total = db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0]
            memory_pending = db.execute("SELECT COUNT(*) FROM memory_jobs WHERE "
                "COALESCE(json_extract(data,'$.state'),'pending') != 'quarantined'").fetchone()[0]
        external = db.execute('SELECT COUNT(*) FROM native_requests').fetchone()[0] if 'native_requests' in tables else 0
        return QueueInspection(1, 'indexed-json' if indexed else 'legacy-json', identity, cursor,
                               pending, memory_pending, memory_total, has_intent, external, instance_id)
