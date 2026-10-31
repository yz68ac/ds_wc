"""Transactional candidate processing, batched decisions and safe recovery; offline only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support.synthetic import NOW, msg
from wxbridge.storage.journal import Journal, open_readonly


class ParticipationStorageTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'.test-tmp'
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'queue.sqlite3'
        self.identity = dict(account='wxid_bot', chat='synthetic@chatroom')
        self.journal = self.open()
        self.addCleanup(lambda: self.journal.close())
        self.limits = dict(max_pending=32, max_pending_per_sender=8)

    def open(self):
        with patch('wxbridge.storage.journal.checked_path', side_effect=lambda p, **kw: Path(p)):
            return Journal(str(self.path), self.identity, 0)

    def intake(self, rows, now=NOW):
        return self.journal.intake_participation(rows, lambda m: m['sender'] != 'wxid_bot', now,
            ttl_seconds=90, limits=self.limits, describe=lambda m: dict(local_id=m['local_id'], reason='accepted'))

    def reserve(self, now=NOW, **kwargs):
        options = dict(debounce_seconds=0, max_wait_seconds=5, max_batch_messages=8)
        options.update(kwargs)
        return self.journal.reserve_participation(now, **options)

    def records(self):
        return self.journal._participation_records(active=False)

    def test_intake_and_cursor_are_atomic_and_duplicates_are_not_replayed(self):
        rows = [msg(1, NOW, '第一条'), msg(2, NOW, '第二条')]
        def accept(row):
            if row['local_id'] == 2:
                raise RuntimeError('synthetic abort')
            return True
        with self.assertRaises(RuntimeError):
            self.journal.intake_participation(rows, accept, NOW, ttl_seconds=90, limits=self.limits)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.journal.get('cursor'), 0)
        self.intake(list(reversed(rows)))
        self.intake(rows)
        self.assertEqual([r['local_id'] for r in self.records()], [1, 2])
        self.assertEqual(self.journal.get('cursor'), 2)

    def test_per_message_verdicts_and_same_sender_grouping_are_atomic(self):
        rows = [msg(1, NOW, '旁观闲聊', 'wxid_a'), msg(2, NOW, '问题一', 'wxid_b'),
                msg(3, NOW, '补充一', 'wxid_b'), msg(4, NOW, '问题二', 'wxid_c')]
        self.intake(rows)
        snapshot = self.reserve()
        tasks = self.journal.apply_participation(snapshot, [2, 3, 4], NOW+1)
        self.assertEqual([t['trigger_ids'] for t in tasks], [[2, 3], [4]])
        self.assertEqual([t['sender'] for t in tasks], ['wxid_b', 'wxid_c'])
        self.assertTrue(all(t['trigger_kind'] == 'participation' and t['generation'] == 'pending' for t in tasks))
        self.assertEqual([r['state'] for r in self.records()], ['silent', 'approved', 'approved', 'approved'])
        self.assertTrue(all('message' not in r for r in self.records()))
        self.assertEqual(self.journal.get('cursor'), 4)
        with self.assertRaisesRegex(ValueError, 'participation_stale_result'):
            self.journal.apply_participation(snapshot, [2], NOW+2)

    def test_silence_is_final_and_new_arrivals_do_not_invalidate_reserved_work(self):
        self.intake([msg(1, NOW, '之前')])
        snapshot = self.reserve()
        self.intake([msg(2, NOW+1, '新消息')], NOW+1)
        self.assertIsNone(self.reserve(NOW+1))
        self.assertEqual(self.journal.apply_participation(snapshot, [], NOW+1), [])
        self.assertEqual(self.reserve(NOW+1)['source_ids'], [2])

    def test_apply_failure_rolls_back_all_verdicts_tasks_and_sequence(self):
        self.intake([msg(1, NOW, '一', 'wxid_a'), msg(2, NOW, '二', 'wxid_b')])
        snapshot = self.reserve()
        original = self.journal.put
        calls = 0
        def put(task):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic write failure')
            return original(task)
        with patch.object(self.journal, 'put', side_effect=put), self.assertRaises(RuntimeError):
            self.journal.apply_participation(snapshot, [1, 2], NOW+1)
        self.assertEqual(self.journal.tasks(), [])
        self.assertEqual(self.journal.get('task_sequence'), 0)
        self.assertEqual([r['state'] for r in self.records()], ['running', 'running'])
        self.assertEqual(self.journal.participation_status()['counts'], {})
        self.assertEqual(len(self.journal.apply_participation(snapshot, [1, 2], NOW+2)), 2)

    def test_invalid_model_targets_do_not_apply_partial_results(self):
        self.intake([msg(1, NOW, '消息')])
        snapshot = self.reserve()
        for selected in ([True], [1, 1], [999], ['1'], None):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, 'participation_invalid_reply_target'):
                self.journal.apply_participation(snapshot, selected, NOW+1)
            self.assertEqual(self.records()[0]['state'], 'running')
            self.assertEqual(self.journal.tasks(), [])

    def test_reply_deadline_is_stamped_atomically_and_invalid_values_do_not_consume_candidates(self):
        self.intake([msg(1, NOW, '问题')])
        snapshot = self.reserve()
        for deadline in (NOW, NOW-1, True, float('inf'), float('nan'), 'later'):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(ValueError, 'participation_invalid_reply_deadline'):
                self.journal.apply_participation(snapshot, [1], NOW, reply_deadline=deadline)
            self.assertEqual(self.records()[0]['state'], 'running')
        tasks = self.journal.apply_participation(snapshot, [1], NOW+1, reply_deadline=NOW+120)
        self.assertEqual(tasks[0]['reply_expires_at'], NOW+120)
        self.assertEqual(self.journal.task(tasks[0]['id'])['reply_expires_at'], NOW+120)
        self.assertEqual(self.records()[0]['expires_at'], NOW+90)

    def test_snapshot_owner_content_token_and_complete_batch_are_checked(self):
        self.intake([msg(1, NOW, '原话'), msg(2, NOW, '另一条')])
        snapshot = self.reserve()
        wrong_owner = deepcopy(snapshot); wrong_owner['identity']['chat'] = 'other@chatroom'
        wrong_token = deepcopy(snapshot); wrong_token['token'] = 'stale'
        wrong_content = deepcopy(snapshot); wrong_content['messages'][0]['sender'] = 'wxid_spoof'
        partial = deepcopy(snapshot); partial['source_ids'] = partial['source_ids'][:1]; partial['messages'] = partial['messages'][:1]
        for changed in (wrong_owner, wrong_token, wrong_content, partial):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'participation_stale_result'):
                self.journal.apply_participation(changed, [], NOW+1)
        self.assertEqual([r['state'] for r in self.records()], ['running', 'running'])

    def test_sender_drift_and_corrupt_records_fail_closed(self):
        self.intake([msg(1, NOW, '原话')])
        snapshot = self.reserve()
        record = self.records()[0]
        record['sender'] = 'wxid_spoof'
        with self.journal.db:
            self.journal.db.execute('UPDATE participation_messages SET data=? WHERE local_id=1',
                                    (json.dumps(record),))
        with self.assertRaisesRegex(ValueError, 'participation_invalid_queue_state'):
            self.journal.apply_participation(snapshot, [1], NOW+1)
        self.assertEqual(self.journal.tasks(), [])
        self.journal.close()
        with self.assertRaisesRegex(ValueError, 'participation_invalid_queue_state'):
            self.open()
        # Restore a usable handle for fixture cleanup without accepting the corruption.
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute('DELETE FROM participation_messages');db.commit();db.close()
        self.journal = self.open()

    def test_expiration_checked_at_apply_and_not_refreshed_by_reserving(self):
        self.intake([msg(1, NOW-89, '快过期'), msg(2, NOW, '有效')])
        snapshot = self.reserve()
        tasks = self.journal.apply_participation(snapshot, [1, 2], NOW+1)
        self.assertEqual([t['trigger_ids'] for t in tasks], [[2]])
        self.assertEqual([r['state'] for r in self.records()], ['expired', 'approved'])

    def test_intake_during_partial_batch_expiry_does_not_invalidate_live_siblings(self):
        self.intake([msg(1, NOW-89, '快过期'), msg(2, NOW, '有效')])
        snapshot = self.reserve()
        self.intake([msg(3, NOW+1, '新增')], NOW+1)
        self.assertEqual([r['state'] for r in self.records()], ['running', 'running', 'pending'])
        tasks = self.journal.apply_participation(snapshot, [1, 2], NOW+1)
        self.assertEqual([t['trigger_ids'] for t in tasks], [[2]])
        self.assertEqual([r['state'] for r in self.records()], ['expired', 'approved', 'pending'])

    def test_debounce_max_wait_batch_limit_and_fifo(self):
        self.intake([msg(1, NOW, '第一条')])
        self.assertIsNone(self.reserve(NOW+1, debounce_seconds=2))
        for offset in range(1, 5):
            self.intake([msg(offset+1, NOW+offset, '继续')], NOW+offset)
        snapshot = self.reserve(NOW+5, debounce_seconds=2)
        self.assertEqual(snapshot['source_ids'], [1, 2, 3, 4, 5])
        self.journal.apply_participation(snapshot, [], NOW+5)
        self.intake([msg(i, NOW+6, str(i)) for i in range(6, 14)], NOW+6)
        snapshot = self.reserve(NOW+6, debounce_seconds=2, max_batch_messages=3)
        self.assertEqual(snapshot['source_ids'], [6, 7, 8])

    def test_release_for_budget_shrink_preserves_attempt_and_original_age(self):
        self.intake([msg(1, NOW, '一'), msg(2, NOW, '二')])
        snapshot = self.reserve()
        self.assertTrue(self.journal.release_participation(snapshot, NOW+1))
        self.assertFalse(self.journal.release_participation(snapshot, NOW+1))
        self.assertEqual([r['attempts'] for r in self.records()], [0, 0])
        self.assertEqual([r['expires_at'] for r in self.records()], [NOW+90, NOW+90])
        self.assertEqual(self.reserve(NOW+1, max_batch_messages=1)['source_ids'], [1])

    def test_retry_is_bounded_and_never_extended_past_original_expiration(self):
        self.intake([msg(1, NOW, '一')])
        first = self.reserve()
        self.assertTrue(self.journal.fail_participation(first, NOW+1, 'connection_failed'))
        self.assertFalse(self.journal.fail_participation(first, NOW+1, 'connection_failed'))
        self.assertIsNone(self.reserve(NOW+2))
        second = self.reserve(NOW+3)
        self.assertEqual(second['source_ids'], [1])
        self.journal.fail_participation(second, NOW+4, 'connection_failed')
        self.assertEqual(self.records()[0]['state'], 'error')
        self.assertIsNone(self.reserve(NOW+10))

    def test_recovery_invalidates_old_token_and_keeps_retry_budget(self):
        self.intake([msg(1, NOW, '一')])
        first = self.reserve()
        self.journal.close(); self.journal = self.open()
        self.journal.recover(NOW+1)
        with self.assertRaises(ValueError):
            self.journal.apply_participation(first, [1], NOW+1)
        second = self.reserve(NOW+1)
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(self.records()[0]['expires_at'], NOW+90)
        self.journal.recover(NOW+2)
        self.assertEqual(self.records()[0]['state'], 'error')

    def test_capacity_backpressure_leaves_unread_source_and_does_not_block_reply_head(self):
        self.limits['max_pending'] = 2
        self.intake([msg(1, NOW, '一'), msg(2, NOW, '二'), msg(3, NOW, '三')])
        self.assertEqual(self.journal.get('cursor'), 2)
        self.assertEqual(self.journal.get('backpressure')['local_id'], 3)
        snapshot = self.reserve()
        tasks = self.journal.apply_participation(snapshot, [1], NOW+1)
        self.intake([msg(3, NOW+1, '三')], NOW+1)
        self.assertEqual(self.journal.get('cursor'), 3)
        self.assertEqual(self.journal.head()['id'], tasks[0]['id'])
        self.assertEqual(self.journal.participation_status()['pending'], 1)

    def test_candidate_bodies_are_not_exposed_in_status_and_metadata_is_merged(self):
        self.intake([msg(1, NOW, 'PRIVATE_BODY')])
        with self.journal.db:
            self.journal.set('participation', dict(enabled=True, outcome='waiting', error='', last_duration_ms=35))
        value = self.journal.status()['participation']
        self.assertEqual(value['last_duration_ms'], 35)
        self.assertEqual(value['state_counts']['pending'], 1)
        self.assertNotIn('PRIVATE_BODY', json.dumps(self.journal.status()))

    def test_cancel_invalidates_results_and_pruning_does_not_replay_sources(self):
        self.intake([msg(1, NOW, '一'), msg(2, NOW, '二')])
        snapshot = self.reserve()
        self.journal.cancel_participation(NOW+1)
        with self.assertRaises(ValueError):
            self.journal.apply_participation(snapshot, [1], NOW+1)
        self.journal.prune_completed(0)
        self.assertEqual(self.records(), [])
        self.intake([msg(1, NOW, '一'), msg(2, NOW, '二')], NOW+1)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.journal.participation_status()['counts']['cancelled'], 2)

    def test_one_time_legacy_migration_preserves_intent_and_external_tasks(self):
        rows = [msg(1, NOW, '未发出的旧问题'), msg(2, NOW, '已提交旧回复')]
        self.journal.intake(rows, lambda m: True, NOW, 0)
        self.journal.seal(NOW)
        old = self.journal.tasks()
        with self.journal.db:
            submitted = old[1]
            submitted.update(generation='ready', chunks=['待确认正文'], next_chunk=0,
                             intent=dict(kind='reply', baseline=2, part=0, confirm_timeout=10))
            self.journal.put(submitted)
        external = self.journal.enqueue_external(dict(request_id='synthetic-external', chat_id='filehelper', text='外部发送'),
            '合成', NOW, max_pending=16, max_requests=16)
        external_before = deepcopy(self.journal.task(external['task_id']))
        submitted_before = deepcopy(self.journal.task(submitted['id']))
        self.journal.migrate_participation(NOW+1, 90)
        self.assertEqual(self.journal.task(old[0]['id'])['cancelled'], 'participation_migrated')
        self.assertEqual(self.journal.task(submitted['id']), submitted_before)
        self.assertEqual(self.journal.task(external['task_id']), external_before)
        self.assertEqual(self.reserve(NOW+1)['source_ids'], [1])
        self.journal.migrate_participation(NOW+2, 90)
        self.assertEqual(len(self.records()), 1)

    def test_unbound_old_short_reply_is_cancelled_without_fabricating_source(self):
        with self.journal.db:
            old = dict(id=self.journal._next_task_id(), sender='', phase='queued', version=1,
                trigger_kind='proactive', triggers=[], trigger_ids=[], generation='ready',
                chunks=['旧的短回复'], next_chunk=0, intent=None, blocked_reason='', deadline=NOW,
                closed_at=NOW, proactive=dict(snapshot_id=12, expires_at=NOW+60))
            self.journal.put(old)
        self.journal.migrate_participation(NOW+1, 90)
        self.assertEqual(self.journal.task(old['id'])['cancelled'], 'participation_unbound_legacy')
        self.assertEqual(self.records(), [])

    def test_legacy_merged_sources_keep_later_messages_and_original_expiration(self):
        self.journal.intake([msg(1, NOW, '第一句')], lambda m: True, NOW, 3)
        self.journal.intake([msg(2, NOW+1, '后补一句')], lambda m: True, NOW+1, 3)
        self.assertEqual(len(self.journal.tasks()), 1)
        self.journal.migrate_participation(NOW+4, 90)
        self.assertEqual(self.reserve(NOW+4)['source_ids'], [1, 2])
        self.assertEqual([r['expires_at'] for r in self.records()], [NOW+90, NOW+91])

    def test_legacy_multipart_with_confirmed_prefix_is_preserved_without_redecision(self):
        row = msg(1, NOW, '旧问题')
        self.journal.intake([row], lambda m: True, NOW, 0)
        self.journal.seal(NOW)
        with self.journal.db:
            task = self.journal.head()
            task.update(generation='ready', chunks=['已确认前缀', '尚未发送后半段'], next_chunk=1,
                        last_ack_id=5, last_confirmation_seconds=.2)
            self.journal.put(task)
        before = deepcopy(task)
        self.journal.migrate_participation(NOW+1, 90)
        self.assertEqual(self.journal.task(task['id']), before)
        self.assertEqual(self.records(), [])

    def test_legacy_acknowledged_search_notice_keeps_history_without_redecision(self):
        self.journal.intake([msg(1, NOW, '旧搜索一'), msg(2, NOW, '旧搜索二')], lambda m: True, NOW, 0)
        self.journal.seal(NOW)
        with self.journal.db:
            tasks = self.journal.tasks()
            tasks[0]['last_notice_ack_id'] = 11
            tasks[1]['notice'] = dict(state='confirmed', text='', ack_id=12)
            for task in tasks:
                self.journal.put(task)
        before = deepcopy(tasks)
        self.journal.migrate_participation(NOW+1, 90)
        self.assertEqual(self.journal.tasks(), before)
        self.assertEqual(self.records(), [])


if __name__ == '__main__':
    unittest.main()
