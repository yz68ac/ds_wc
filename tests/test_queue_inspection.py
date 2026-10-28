"""Read-only queue compatibility and bounded scheduler queries; synthetic data only."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wxbridge.storage.inspection import inspect_queue
from wxbridge.storage.journal import Journal, open_readonly


class QueueInspectionTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'queue.sqlite3'

    def legacy(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE tasks(id INTEGER PRIMARY KEY,data TEXT);CREATE TABLE memory_jobs(id INTEGER PRIMARY KEY,data TEXT);')
            db.executemany('INSERT INTO meta VALUES (?,?)', [('identity', json.dumps(dict(account='synthetic', chat='synthetic@chatroom'))),
                ('cursor', '19'), ('secret', '"KEY_SECRET"')])
            db.executemany('INSERT INTO tasks VALUES (?,?)', [(i, json.dumps(dict(id=i, deadline=10-i,
                phase='done' if i == 1 else 'open' if i == 2 else 'queued',
                intent=dict(part=0) if i == 4 else None, generation='ready', text='PRIVATE_BODY'))) for i in range(1, 6)])
            db.executemany('INSERT INTO memory_jobs VALUES (?,?)', [(1, '{"id":1,"state":"pending","body":"MEMORY_SECRET"}'),
                (2, '{"id":2,"state":"quarantined"}')])
            db.commit()

    def test_legacy_snapshot_never_migrates_or_decodes_task_bodies(self):
        self.legacy()
        before = hashlib.sha256(self.path.read_bytes()).digest()
        original = json.loads
        with patch.object(Journal, '__init__', side_effect=AssertionError('must_not_migrate')), \
             patch('wxbridge.storage.inspection.json.loads', wraps=original) as loads:
            result = inspect_queue(self.path)
        self.assertEqual(loads.call_count, 2)
        self.assertEqual(result.schema, 1)
        self.assertEqual(result.storage_schema, 'legacy-json')
        self.assertEqual((result.cursor, result.pending, result.memory_pending, result.memory_total, result.has_intent), (19, 4, 1, 2, True))
        self.assertEqual(result.external_requests, 0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        for secret in ('KEY_SECRET', 'PRIVATE_BODY', 'MEMORY_SECRET'):
            self.assertNotIn(secret, repr(result))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_xinfo(tasks)')], ['id', 'data'])

    def test_indexed_and_legacy_lookahead_preserve_order_without_decode_for_counts(self):
        self.legacy()
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw:Path(v)):
            legacy = open_readonly(str(self.path))
            self.addCleanup(legacy.close)
            original = json.loads
            with patch('wxbridge.storage.journal.json.loads', wraps=original) as loads:
                self.assertTrue(legacy.has_intent())
                self.assertEqual(legacy.pending_count(), 4)
                self.assertFalse(legacy.has_external_history())
                self.assertEqual(legacy.external_request_count(), 0)
                self.assertEqual(loads.call_count, 0)
                self.assertEqual([t['id'] for t in legacy.generation_candidates(2)], [5, 4])
                self.assertEqual(loads.call_count, 2)
            journal = Journal(str(self.path), dict(account='synthetic', chat='synthetic@chatroom'), 0)
            self.addCleanup(journal.close)
        self.assertTrue(journal.has_external_history())
        self.assertEqual(journal.external_request_count(), 0)
        self.assertEqual([t['id'] for t in journal.generation_candidates(3)], [5, 4, 3])
        snapshot = inspect_queue(self.path)
        self.assertEqual(snapshot.storage_schema, 'indexed-json')
        self.assertEqual(snapshot.pending, 4)
        self.assertTrue(snapshot.has_intent)
        plan = str(journal.db.execute("EXPLAIN QUERY PLAN SELECT data FROM tasks WHERE phase != 'done' ORDER BY deadline,id LIMIT 4").fetchall())
        self.assertIn('generation_order', plan)
        for limit in (True, 0, -1, 2.5):
            with self.assertRaises(ValueError):journal.generation_candidates(limit)

    def test_missing_corrupt_and_future_queues_are_rejected_without_creation(self):
        with self.assertRaises(ValueError):inspect_queue(self.path)
        self.assertFalse(self.path.exists())
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:db.execute('PRAGMA user_version=2')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'unsupported_schema'):inspect_queue(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=0')
            db.execute("UPDATE meta SET value='true' WHERE key='cursor'")
            db.commit()
        with self.assertRaisesRegex(ValueError, 'invalid_metadata'):inspect_queue(self.path)

    def test_instance_identity_is_optional_validated_and_not_added_to_legacy_release_summary(self):
        self.legacy()
        self.assertIsNone(inspect_queue(self.path).instance_id)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO meta VALUES (?,?)', ('instance_id', '"alpha-2"'))
            db.commit()
        snapshot = inspect_queue(self.path)
        self.assertEqual(snapshot.instance_id, 'alpha-2')
        self.assertNotIn('instance_id', snapshot.release_summary())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE meta SET value='\"../invalid\"' WHERE key='instance_id'")
            db.commit()
        with self.assertRaisesRegex(ValueError, 'invalid_instance'):inspect_queue(self.path)

    def test_corrupt_states_fail_closed_for_legacy_and_indexed_queues_without_mutation(self):
        states = [dict(phase='queued', intent=value) for value in ('PRIVATE_INVALID', '', [1], [], True, False, 1)]
        states += [dict(phase=value, intent=None) for value in ('unknown', None, False, [])]
        states += [dict(intent=None), dict(phase='queued'), dict(phase='done',intent=dict(part=0))]
        for indexed in (False, True):
            for number, state in enumerate(states):
                with self.subTest(indexed=indexed, case=number):
                    folder=Path(self.temp.name)/f'queue-{indexed}-{number}'
                    folder.mkdir()
                    self.path=folder/'queue.sqlite3'
                    self.legacy()
                    with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw:Path(v)):
                        if indexed:
                            Journal(str(self.path),dict(account='synthetic',chat='synthetic@chatroom'),0).close()
                        with closing(sqlite3.connect(self.path)) as db:
                            db.execute('UPDATE tasks SET data=? WHERE id=4',(json.dumps(state),))
                            db.commit()
                        before=self.path.read_bytes()
                        for operation in (lambda:inspect_queue(self.path), lambda:open_readonly(str(self.path)),
                                lambda:Journal(str(self.path),dict(account='synthetic',chat='synthetic@chatroom'),0)):
                            with self.assertRaisesRegex(ValueError,'^queue_invalid_task_state$'):operation()
                            self.assertEqual(self.path.read_bytes(),before)

    def test_writer_rejects_invalid_states_before_replacing_existing_tasks(self):
        self.legacy()
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw:Path(v)):
            journal=Journal(str(self.path),dict(account='synthetic',chat='synthetic@chatroom'),0)
        self.addCleanup(journal.close)
        original=journal.task(4)
        for changes in (dict(intent='PRIVATE_INVALID'),dict(intent=[]),dict(intent=True),dict(phase=None),
                dict(phase='future'),dict(phase='done')):
            with self.assertRaisesRegex(ValueError,'^queue_invalid_task_state$'):
                journal.put(dict(original,**changes))
            self.assertEqual(journal.task(4),original)
        missing=dict(original);missing.pop('intent')
        with self.assertRaisesRegex(ValueError,'^queue_invalid_task_state$'):journal.put(missing)

    def test_malformed_legacy_json_reports_safe_state_error(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE tasks SET data='PRIVATE_INVALID_JSON' WHERE id=4")
            db.commit()
        with self.assertRaisesRegex(ValueError,'^queue_invalid_task_state$'):inspect_queue(self.path)
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda v, **kw:Path(v)):
            with self.assertRaisesRegex(ValueError,'^queue_invalid_task_state$'):open_readonly(str(self.path))


if __name__ == '__main__':unittest.main()
