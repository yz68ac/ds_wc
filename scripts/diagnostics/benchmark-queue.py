"""Synthetic scheduler-query cost at 1/4/16 groups and 100/1000 active tasks each.

Uses only in-memory SQLite. Reports local observations, never timing pass/fail limits.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from wxbridge.storage.journal import Journal


def queue(size):
    journal = Journal.__new__(Journal)
    journal.db = sqlite3.connect(':memory:')
    journal.db.executescript('CREATE TABLE tasks(id INTEGER PRIMARY KEY,data TEXT);'
                            'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);')
    with journal.db:
        journal._migrate_tasks()
        for identifier in range(1, size+1):
            journal.put(dict(id=identifier, phase='queued', generation='pending', deadline=identifier,
                             intent=None, triggers=[dict(text='synthetic '*100)]))
    return journal


def measure(journals, old):
    decoded = queries = 0
    original = json.loads
    def loads(*args, **kwargs):
        nonlocal decoded
        decoded += 1
        return original(*args, **kwargs)
    def traced(_):
        nonlocal queries
        queries += 1
    for journal in journals:journal.db.set_trace_callback(traced)
    started = time.perf_counter()
    with patch('wxbridge.storage.journal.json.loads', new=loads):
        for journal in journals:
            if old:
                sorted(journal.tasks(), key=lambda t:(t['deadline'], t['id']))
                sorted(journal.tasks(), key=lambda t:(t['deadline'], t['id']))[:4]
                any(t.get('intent') for t in journal.tasks())
                len(journal.tasks())
            else:
                journal.generation_candidates(4)
                journal.has_intent()
                journal.pending_count()
    elapsed = time.perf_counter()-started
    for journal in journals:journal.db.set_trace_callback(None)
    return dict(elapsed_ms=round(elapsed*1000, 3), decoded_tasks=decoded, sql_queries=queries)


def main():
    results = []
    for groups in (1, 4, 16):
        for pending in (100, 1000):
            journals = [queue(pending) for _ in range(groups)]
            try:
                results.append(dict(groups=groups, pending_per_group=pending,
                                    old=measure(journals, True), bounded=measure(journals, False)))
            finally:
                for journal in journals:journal.close()
    print(json.dumps(dict(synthetic=True, includes_store_refresh=False, results=results), indent=2))


if __name__ == '__main__':main()
