"""Synthetic regressions for review R3-R10; no network or production data."""
from tests.support.model_support import default_config
from contextlib import nullcontext
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
from wxbridge.ai.participation import evaluate
from wxbridge.storage.journal import Journal
import tests.support.memory as memories
import tests.support.media as media
import tests.support.native_api as native
import tests.support.pipeline as pipeline
from tests.support.synthetic import NOW, msg


class ReviewBoundaryTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls();case.setUp();self.addCleanup(case.doCleanups)
        return case

    def test_unavailable_memory_source_is_quarantined_and_does_not_hold_next_job(self):
        case=self.fixture(memories.MemoryPipelineFixture);case.enable();case.confirmed()
        case.bot.memory.job[2].result=dict(ok=False,error='JSONDecodeError');case.bot.memory.step()
        newer=case.add(3,mention=False)
        with case.bot.journal.db:
            case.bot.journal.enqueue_memory('reply','second',[dict(local_id=3,time=newer['time'])])
        case.store.messages_in_window.side_effect=lambda *args:[newer]
        for _ in range(4):
            case.now=case.bot.journal.memory_jobs()[0]['retry_at']+1
            case.bot.memory.step()
        self.assertEqual(case.bot.journal.status()['memory_quarantined'][0]['attempts'],5)
        self.assertEqual(case.bot.journal.memory_jobs()[0]['key'],'second')
        self.assertEqual(case.bot.memory.store.data['watermark'],0)
        case.bot.memory.step()
        self.assertEqual(case.bot.memory.job[0]['key'],'second')

    def test_confirmation_has_page_budget_throttle_and_bounded_old_row_audit(self):
        case=self.fixture(native.JournalFixture);identifier=case.normal_task()
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
        case=self.fixture(native.JournalFixture)
        records=[msg(n,NOW,'synthetic') for n in range(89,93)]
        limits=dict(max_pending=2,max_pending_per_sender=2)
        case.journal.intake_participation(records,lambda _:True,NOW,ttl_seconds=90,limits=limits)
        self.assertEqual(case.journal.participation_status()['pending'],2)
        self.assertFalse(case.journal.tasks())
        self.assertEqual(case.journal.get('cursor'),90)
        self.assertEqual(case.journal.get('intake_deferred')['local_id'],91)
        batch=case.journal.reserve_participation(NOW,debounce_seconds=0,max_wait_seconds=5,max_batch_messages=2)
        case.journal.apply_participation(batch,[],NOW)
        case.journal.intake_participation(records,lambda _:True,NOW+10,ttl_seconds=90,limits=limits)
        self.assertEqual(case.journal.get('cursor'),92)
        self.assertIsNone(case.journal.get('backpressure'))
        self.assertEqual(case.journal.participation_status()['counts'],dict(silent=2))

    def test_deferred_source_is_recorded_as_expired_without_resetting_original_age(self):
        case=self.fixture(pipeline.PipelineFixture)
        case.cfg['queue'].update(max_pending=1,max_pending_per_sender=1)
        case.add(1);case.add(2);case.bot.tick()
        self.assertEqual(case.bot.last_local_id,1)
        case.now+=100
        self.assertTrue(case.bot._accept(case.rows[1],case.now))
        with case.bot.journal.db:
            for task in case.bot.journal.tasks():
                task.update(phase='done',version=task['version']+1)
                case.bot.journal.put(task)
        case.bot.tick()
        self.assertEqual(case.bot.last_local_id,2)
        state=case.bot.journal.participation_status()
        self.assertEqual(state['counts'].get('expired'),1)
        self.assertEqual(state['recent'][0]['expires_at'],NOW+90)

    def test_stale_and_unavailable_indexes_use_the_proven_cache(self):
        case=self.fixture(media.ImageBindingFixture)
        case.store._query_messages.return_value=[dict(type=3,image_md5=case.digest,sender='friend',image_cache_key='c'*32)]
        path=case.root/'msg/attach'/case.bucket/'2026-10/Img'/('c'*32+'_t.dat')
        path.parent.mkdir(parents=True);path.write_bytes(media.png())
        ref=dict(server_id='123',sender='friend',image_md5=case.digest,chat_id=case.chat)
        for unavailable in (False,True):
            with self.subTest(unavailable=unavailable):
                if unavailable:case.store._connect=Mock(side_effect=sqlite3.DatabaseError())
                binding=case.store.image_binding(case.chat,ref)
                data,info=read_local_image(binding, max_bytes=DEFAULTS['vision']['max_image_bytes'])
                self.assertEqual(data,media.png())
                self.assertEqual(info['binding_source'],'verified_message_cache')

    def test_invalid_backend_and_nonfinite_deadlines_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'.test-tmp') as name,nullcontext():
            path=Path(name)/'config.json'
            for value in (dict(ui=dict(backend='native-service')),
                          dict(ui=dict(send_confirm_timeout_seconds=float('nan'))),
                          dict(poll_interval_seconds=float('inf'))):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):load_config(path)

    def test_approved_question_becomes_full_reply_without_rewinding_newer_intake(self):
        case=self.fixture(native.JournalFixture)
        source=msg(89,NOW,'看看这张图');source['reference']=dict(kind='image',available=False,text='')
        limits=dict(max_pending=32,max_pending_per_sender=8)
        case.journal.intake_participation([source],lambda _:True,NOW,ttl_seconds=90,limits=limits)
        batch=case.journal.reserve_participation(NOW,debounce_seconds=0,max_wait_seconds=5,max_batch_messages=8)
        case.journal.intake_participation([msg(90,NOW,'另一个问题')],lambda _:True,NOW,ttl_seconds=90,limits=limits)
        self.assertFalse(case.journal.tasks())
        task=case.journal.apply_participation(batch,[89],NOW)[0]
        self.assertEqual(task['trigger_kind'],'participation')
        self.assertEqual(task['generation'],'pending')
        self.assertEqual(task['triggers'][0]['reference']['kind'],'image')
        self.assertEqual(case.journal.get('cursor'),90)
        self.assertEqual(case.journal.participation_status()['pending'],1)
        with self.assertRaises(ValueError):case.journal.apply_participation(batch,[89],NOW)
        self.assertEqual(len(case.journal.tasks()),1)

    def test_batch_decision_uses_only_the_participation_call(self):
        cfg=default_config()
        client=Mock();client.complete.return_value=dict(content='{"reply_to":[7,8]}')
        with patch('wxbridge.ai.client.client_for',return_value=client):
            decision=evaluate(cfg,[],source_ids=[7,8])
        self.assertEqual(decision,dict(reply_to=[7,8]));self.assertEqual(client.complete.call_count,1)
