"""队列只读状态、部署权限校验和 Linux 单实例锁。"""
from __future__ import annotations

from pathlib import Path
import os
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.storage.journal import Journal, checked_path, read_status
from wxbridge.app.runtime import instance_lock


class JournalSafetyTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.sqlite3"

    def test_production_rejects_disk_directory(self):
        # Windows 直接拒绝；Linux /tmp 或工作区也不是 noswap tmpfs。
        with self.assertRaises(ValueError):
            checked_path(str(self.path))

    def open_journal(self):
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw: Path(v)):
            journal = Journal(str(self.path), {'account': 'synthetic', 'chat': 'synthetic'}, 8)
        self.addCleanup(journal.close)
        return journal

    @staticmethod
    def task(identifier, phase='done', intent=None):
        return dict(id=identifier, phase=phase, intent=intent, deadline=identifier,
                    generation='ready', version=1, chunks=['合成回复'], triggers=[],
                    next_chunk=0, blocked_reason='', sender='synthetic')

    def test_legacy_schema_migration_keeps_cursor_intent_and_old_writer_compatibility(self):
        intent = dict(part=0, baseline=9, deadline=123, confirm_timeout=10)
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE tasks(id INTEGER PRIMARY KEY,data TEXT NOT NULL);'
                             'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
            db.execute('INSERT INTO tasks VALUES (?,?)', (21, json.dumps(self.task(21, 'queued', intent))))
            db.execute('INSERT INTO meta VALUES (?,?)', ('cursor', '19'))
            db.execute('INSERT INTO meta VALUES (?,?)', ('identity', json.dumps({'account': 'synthetic', 'chat': 'synthetic'})))
        db.close()
        journal = self.open_journal()
        self.assertEqual(journal.get('cursor'), 19)
        self.assertEqual(journal.head()['intent'], intent)
        with journal.db:
            # 旧版回退仍可用二列 VALUES 写入；生成列与索引自动更新。
            journal.db.execute('INSERT INTO tasks VALUES (?,?)', (22, json.dumps(self.task(22))))
        self.assertEqual(journal.task(22)['phase'], 'done')
        self.assertEqual(journal.head()['id'], 21)

    def test_active_queries_decode_only_live_rows_and_use_index(self):
        journal = self.open_journal()
        with journal.db:
            for identifier in range(1, 1001):
                journal.put(self.task(identifier))
            journal.put(self.task(1001, 'queued'))
        original = json.loads
        with patch('wxbridge.storage.journal.json.loads', wraps=original) as loads:
            self.assertEqual(journal.head()['id'], 1001)
            self.assertEqual(loads.call_count, 1)
            self.assertEqual(len(journal.tasks()), 1)
            self.assertEqual(loads.call_count, 2)
        plan = str(journal.db.execute("EXPLAIN QUERY PLAN SELECT data FROM tasks "
                    "INDEXED BY active_tasks WHERE phase != 'done' ORDER BY id").fetchall())
        self.assertIn('active_tasks', plan)

    def test_retention_protects_intents_and_monotonic_ids_across_reopen(self):
        journal = self.open_journal()
        with journal.db:
            for identifier in range(1, 5):
                journal.put(self.task(identifier))
            journal.set('task_sequence', 4)
            journal.put(self.task(5, 'queued', dict(part=0)))
        journal.prune_completed(0, [4])
        self.assertEqual([t['id'] for t in journal.tasks(unfinished=False)], [4, 5])
        self.assertTrue(journal.head()['intent'])
        journal.prune_completed(0)
        self.assertEqual([t['id'] for t in journal.tasks(unfinished=False)], [5])
        with journal.db:
            # 迁移时会校正最大 ID；运行时新任务的分配在同一事务单调推进。
            journal.set('task_sequence', 5)
            self.assertEqual(journal._next_task_id(), 6)
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw: Path(v)):
            reopened = Journal(str(self.path), {'account': 'synthetic', 'chat': 'synthetic'}, 0)
        try:
            with reopened.db:
                self.assertEqual(reopened._next_task_id(), 7)
        finally:
            reopened.close()

    def test_confirmation_policy_failure_rolls_back_ack_and_memory_job(self):
        journal = self.open_journal()
        task = self.task(1, 'queued', dict(part=0))
        task['triggers'] = [dict(local_id=1, time=100)]
        with journal.db:
            journal.put(task)
        def failing(*args):
            raise ValueError('synthetic_policy_failure')
        with self.assertRaises(ValueError):
            journal.acknowledge(1, 2, 101, memory=True, on_confirm=failing)
        self.assertTrue(journal.task(1)['intent'])
        self.assertEqual(journal.task(1)['next_chunk'], 0)
        self.assertFalse(journal.memory_jobs())

    def test_fallback_confirmation_never_schedules_memory(self):
        journal = self.open_journal()
        task = self.task(1, 'queued', dict(part=0))
        task.update(generation_failed=True, triggers=[dict(local_id=1, time=100)])
        with journal.db:
            journal.put(task)
        policy = unittest.mock.Mock()
        journal.acknowledge(1, 2, 101, memory=True, on_confirm=policy)
        self.assertEqual(journal.task(1)['phase'], 'done')
        self.assertFalse(journal.memory_jobs())
        policy.assert_not_called()

    def test_silent_completion_does_not_update_cooldown_or_enqueue_memory(self):
        journal = self.open_journal()
        task = self.task(1, 'queued')
        task['triggers'] = [dict(local_id=1, time=100)]
        with journal.db:
            journal.put(task)
        self.assertTrue(journal.generated(1, 1, [], search=dict(attempted=True, calls=1, results=1, errors=['empty_reply'])))
        self.assertEqual(journal.task(1)['phase'], 'done')
        self.assertIsNone(journal.head())
        self.assertEqual(journal.get('last_success'), 0)
        self.assertFalse(journal.memory_jobs())
        self.assertIn('empty_reply', journal.get('last_search')['errors'])

    def test_readonly_legacy_status_does_not_migrate_database(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE tasks(id INTEGER PRIMARY KEY,data TEXT NOT NULL);'
                             'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                             'CREATE TABLE memory_jobs(id INTEGER PRIMARY KEY,data TEXT NOT NULL);')
            db.execute('INSERT INTO tasks VALUES (?,?)', (1, json.dumps(self.task(1, 'queued'))))
            db.execute('INSERT INTO meta VALUES (?,?)', ('cursor', '17'))
        db.close()
        before = self.path.read_bytes()
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw: Path(v)):
            self.assertEqual(read_status(str(self.path))['queue_length'], 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_queue_status_is_read_only_and_does_not_expose_payload(self):
        with patch("wxbridge.storage.journal.checked_path", side_effect=lambda value, **kw: Path(value)):
            journal = Journal(str(self.path), {"account": "self", "chat": "test"}, 19)
            with journal.db:
                journal.set("secret", "sk-NOT-A-REAL-KEY")
            before = self.path.read_bytes()
            status = read_status(str(self.path))
            self.assertEqual(status["cursor"], 19)
            self.assertNotIn("secret", str(status))
            self.assertEqual(self.path.read_bytes(), before)
            journal.close()

    @unittest.skipUnless(sys.platform == "linux", "实际 fcntl 锁在 Linux 验证")
    def test_single_instance_lock_excludes_second_listener(self):
        with patch("wxbridge.app.runtime.checked_path", side_effect=lambda value: Path(value)):
            with instance_lock(str(self.path)):
                with self.assertRaisesRegex(ValueError, "已有监听"):
                    with instance_lock(str(self.path)):
                        self.fail("second listener entered")
            with instance_lock(str(self.path)):
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
