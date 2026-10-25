"""Synthetic regressions for review R3-R10; no network or production data."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.images import read_local_image
from wxbridge.config import DEFAULTS, load_config
from wxbridge.ai.proactive import evaluate
from wxbridge.storage.journal import Journal
import test_memory as memories
import test_media as media
import test_native_api as native
import test_context_pipeline as pipeline
from synthetic import NOW, msg


class ReviewBoundaryTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls();case.setUp();self.addCleanup(case.doCleanups)
        return case

    def test_unavailable_memory_source_is_quarantined_and_does_not_hold_next_job(self):
        case=self.fixture(memories.MemoryPipelineTests);case.enable();case.confirmed()
        case.bot.memory_job[2].result=dict(ok=False,error='JSONDecodeError');case.bot._memory_step()
        newer=case.add(3,mention=False)
        with case.bot.journal.db:
            case.bot.journal.enqueue_memory('reply','second',[dict(local_id=3,time=newer['time'])])
        case.store.messages_in_window.side_effect=lambda *args:[newer]
        for _ in range(4):
            case.now=case.bot.journal.memory_jobs()[0]['retry_at']+1
            case.bot._memory_step()
        self.assertEqual(case.bot.journal.status()['memory_quarantined'][0]['attempts'],5)
        self.assertEqual(case.bot.journal.memory_jobs()[0]['key'],'second')
        self.assertEqual(case.bot.memory.data['watermark'],0)
        case.bot._memory_step()
        self.assertEqual(case.bot.memory_job[0]['key'],'second')

    def test_confirmation_has_page_budget_throttle_and_bounded_old_row_audit(self):
        case=self.fixture(native.JournalTests);identifier=case.normal_task()
        job=Mock();job.poll.return_value=dict(ok=True,ui=dict(operation=dict(submitted=True)))
        case.delivery.send(Mock(return_value=job));case.delivery.poll_result()
        rows=[dict(local_id=n,sender='other',type=1,text='synthetic') for n in range(3,1003)]
        case.store.messages_after.side_effect=lambda chat,cursor,limit:[m for m in rows if m['local_id']>cursor][:limit]
        deadline=case.journal.task(identifier)['intent']['deadline']
        with patch('wxbridge.app.delivery.time.time',return_value=deadline+1):
            case.delivery.confirm();self.assertEqual(case.store.messages_after.call_count,2)
            case.delivery.confirm();self.assertEqual(case.store.messages_after.call_count,2)
        intent=case.journal.task(identifier)['intent']
        self.assertEqual(intent['scan_cursor'],202)
        # A late fill of an older row is found by the audit, without resubmitting.
        rows[0].update(sender='synthetic',text='合成回复')
        with patch('wxbridge.app.delivery.time.time',return_value=deadline+17):case.delivery.confirm()
        self.assertEqual(case.journal.task(identifier)['phase'],'done')

    def test_capacity_preserves_cursor_and_defers_instead_of_dropping(self):
        case=self.fixture(native.JournalTests)
        records=[msg(n,NOW,'synthetic') for n in range(89,93)]
        case.journal.intake(records,lambda _:True,NOW,0,limits=dict(max_pending=2,max_pending_per_sender=2))
        self.assertEqual(len(case.journal.tasks()),2)
        self.assertEqual(case.journal.get('cursor'),90)
        self.assertEqual(case.journal.get('intake_deferred')['local_id'],91)
        with case.journal.db:
            for task in case.journal.tasks():task['phase']='done';case.journal.put(task)
        case.journal.intake(records,lambda _:True,NOW+200,0,limits=dict(max_pending=2,max_pending_per_sender=2))
        self.assertEqual(case.journal.get('cursor'),92)
        self.assertIsNone(case.journal.get('backpressure'))

    def test_deferred_fresh_trigger_is_not_dropped_after_waiting_over_90_seconds(self):
        case=self.fixture(pipeline.PipelineTests)
        case.cfg['queue'].update(debounce_seconds=0,max_pending=1,max_pending_per_sender=1)
        case.add(1);case.add(2);case.bot.tick()
        self.assertEqual(case.bot.last_local_id,1)
        case.now+=100
        self.assertTrue(case.bot._accept(case.rows[1],case.now))

    def test_stale_and_unavailable_indexes_use_the_proven_cache(self):
        case=self.fixture(media.ImageBindingTests)
        case.store._query_messages.return_value=[dict(type=3,image_md5=case.digest,sender='friend',image_cache_key='c'*32)]
        path=case.root/'msg/attach'/case.bucket/'2026-10/Img'/('c'*32+'_t.dat')
        path.parent.mkdir(parents=True);path.write_bytes(media.png())
        ref=dict(server_id='123',sender='friend',image_md5=case.digest,chat_id=case.chat)
        for unavailable in (False,True):
            with self.subTest(unavailable=unavailable):
                if unavailable:case.store._connect=Mock(side_effect=sqlite3.DatabaseError())
                binding=case.store.image_binding(case.chat,ref)
                data,info=read_local_image(binding,DEFAULTS)
                self.assertEqual(data,media.png())
                self.assertEqual(info['binding_source'],'verified_message_cache')

    def test_invalid_backend_and_nonfinite_deadlines_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'.test-tmp') as name,patch('wxbridge.config.load_dotenv'):
            path=Path(name)/'config.json'
            for value in (dict(ui=dict(backend='native-service')),
                          dict(ui=dict(send_confirm_timeout_seconds=float('nan'))),
                          dict(queue=dict(debounce_seconds=float('inf')))):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):load_config(path)

    def test_addressed_approved_question_becomes_full_reply_task_without_advancing_cursor(self):
        case=self.fixture(native.JournalTests)
        source=msg(50,NOW,'ds 看看这张图');source['reference']=dict(kind='image',available=False,text='')
        task=case.journal.enqueue_addressed(source,NOW)
        self.assertEqual(task['trigger_kind'],'addressed')
        self.assertEqual(task['generation'],'pending')
        self.assertEqual(task['triggers'][0]['reference']['kind'],'image')
        self.assertEqual(case.journal.get('cursor'),88)
        self.assertIsNone(case.journal.enqueue_addressed(source,NOW))

    def test_named_handoff_uses_only_the_participation_call(self):
        cfg=copy.deepcopy(DEFAULTS)
        client=Mock();client.complete.return_value=dict(content='{"speak":true,"reply_to":7}')
        with patch('wxbridge.ai.client.DeepSeekClient',return_value=client):
            decision=evaluate(cfg,[],source_ids=[7],judgment_only=True)
        self.assertTrue(decision['handoff']);self.assertEqual(client.complete.call_count,1)
