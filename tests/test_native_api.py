"""Synthetic native API tests. No live config, Frida attachment, models or WeChat sends."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import time
import types
import sys
import unittest
from unittest.mock import Mock, patch

from wxbridge.app.delivery import DeliveryController
from wxbridge.config import DEFAULTS
from wxbridge.native import helper
from wxbridge.native.api import NativeAPI
from wxbridge.native.client import NativeClient
from wxbridge.native.protocol import NativeError, decode, encode, receive, settings, validate_send
from wxbridge.storage.journal import Journal


def payload(identifier='native-synthetic-1', text='中文第一行[偷笑]\n第二行😀', chat='filehelper'):
    return dict(op='send_text',request_id=identifier,chat_id=chat,text=text)


class ProtocolTests(unittest.TestCase):
    def test_unicode_newline_and_exact_text_are_preserved(self):
        value=payload(text='  中文😀\n[偷笑] **保留原文**  ')
        self.assertEqual(validate_send(value),value)
        frame=encode(value)
        self.assertEqual(decode(frame[4:]),value)
        self.assertEqual(struct.unpack('!I',frame[:4])[0],len(frame)-4)

    def test_utf8_limit_is_bytes_not_characters(self):
        self.assertEqual(validate_send(payload(text='中'*1365))['text'],'中'*1365)
        for text in ['中'*1366,'😀'*1025]:
            with self.subTest(text_bytes=len(text.encode())):
                with self.assertRaisesRegex(NativeError,'text_too_large'):validate_send(payload(text=text))

    def test_invalid_request_types_ids_and_text(self):
        values=[None,[],{},dict(payload(),extra=1),dict(payload(),op='eval'),
                dict(payload(),request_id='../escape'),dict(payload(),request_id=''),
                dict(payload(),chat_id='显示名称'),dict(payload(),chat_id='/proc/1'),
                dict(payload(),text=''),dict(payload(),text=' \n'),dict(payload(),text='a\0b'),
                dict(payload(),text='\ud800'),dict(payload(),text=1)]
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(NativeError):validate_send(value)

    def test_invalid_frames_do_not_echo_input(self):
        for value in [b'[]',b'{',b'{"text":NaN}',b'\xff']:
            with self.assertRaisesRegex(NativeError,'invalid_frame'):decode(value)
        with self.assertRaisesRegex(NativeError,'frame_too_large'):encode({'x':'x'*40000})

    def test_fragmented_frames_and_oversized_prefix(self):
        class Stream:
            def __init__(self,body):self.body=body
            def recv(self,count):
                result=self.body[:min(count,2)];self.body=self.body[len(result):];return result
        self.assertEqual(receive(Stream(encode(payload()))),payload())
        with self.assertRaisesRegex(NativeError,'frame_too_large'):receive(Stream(struct.pack('!I',999999)))
        with self.assertRaisesRegex(NativeError,'connection_closed'):receive(Stream(b'\0'))

    def test_config_validation_and_default_off(self):
        self.assertFalse(settings(DEFAULTS)['enabled'])
        for key,value in [('enabled',1),('max_text_bytes',4097),('max_pending',0),
                          ('allowed_chat_ids',['nickname']),('socket','/tmp/'+'x'*110)]:
            cfg=copy.deepcopy(DEFAULTS);cfg['native_api'][key]=value
            with self.subTest(key=key),self.assertRaises(NativeError):settings(cfg)
        cfg=copy.deepcopy(DEFAULTS);cfg['native_api']['helper_socket']=cfg['native_api']['socket']
        with self.assertRaises(NativeError):settings(cfg)

    def test_privileged_helper_revalidates_input_and_allowlist(self):
        self.assertEqual(helper.validate(payload(),['filehelper']),len(payload()['text'].encode()))
        with self.assertRaisesRegex(ValueError,'native_chat_not_allowed'):
            helper.validate(payload(chat='synthetic@chatroom'),['filehelper'])
        with self.assertRaisesRegex(ValueError,'native_invalid_request'):
            helper.validate(payload(text='\ud800'),['filehelper'])


class JournalTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'queue.sqlite3'
        self.identity={'account':'synthetic','chat':'synthetic@chatroom'}
        self.journal=self.open();self.addCleanup(lambda:self.journal.close())
        self.cfg=copy.deepcopy(DEFAULTS);self.cfg['ui']['dry_run']=False
        self.cfg['target']['cooldown_seconds']=0
        self.store=Mock(self_wxid='synthetic');self.store.last_local_id.return_value=2
        self.delivery=DeliveryController(self.cfg,self.journal,self.store,'synthetic@chatroom','合成群')

    def open(self):
        with patch('wxbridge.storage.journal.checked_path',side_effect=lambda p,**kw:Path(p)):
            return Journal(str(self.path),self.identity,88)

    def enqueue(self,value=None):
        return self.journal.enqueue_external(value or payload(),'File Transfer',100,
                                             max_pending=16,max_requests=1024)

    def test_transactional_enqueue_does_not_advance_group_cursor(self):
        accepted=self.enqueue();task=self.journal.task(accepted['task_id'])
        self.assertEqual(task['phase'],'external');self.assertEqual(task['generation'],'ready')
        self.assertEqual(task['chunks'],[payload()['text']]);self.assertEqual(self.journal.get('cursor'),88)
        self.assertEqual(self.journal.get('identity'),self.identity)

    def test_duplicate_survives_restart_and_conflicting_body_or_target_is_rejected(self):
        first=self.enqueue();self.journal.close();self.journal=self.open()
        duplicate=self.enqueue()
        self.assertEqual(first['task_id'],duplicate['task_id']);self.assertTrue(duplicate['duplicate'])
        for value in [payload(text='其他合成正文'),payload(chat='synthetic@chatroom')]:
            with self.assertRaisesRegex(NativeError,'request_id_conflict'):self.enqueue(value)
        self.assertEqual(len(self.journal.tasks()),1)

    def test_limits_do_not_evict_dedupe_records_or_reject_existing_id(self):
        self.enqueue()
        with self.assertRaisesRegex(NativeError,'queue_full'):
            self.journal.enqueue_external(payload('second'),'File Transfer',101,max_pending=1,max_requests=1024)
        self.assertTrue(self.journal.enqueue_external(payload(),'File Transfer',101,max_pending=1,max_requests=1)['duplicate'])
        with self.assertRaisesRegex(NativeError,'request_history_full'):
            self.journal.enqueue_external(payload('second'),'File Transfer',101,max_pending=16,max_requests=1)

    def test_failed_enqueue_rolls_back_request_and_sequence(self):
        with patch.object(self.journal,'put',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.enqueue()
        self.assertEqual(self.journal.get('task_sequence'),0)
        self.assertIsNone(self.journal.external_duplicate(payload()))

    def test_unknown_request_returns_only_fixed_code(self):
        with self.assertRaisesRegex(NativeError,'request_not_found'):self.journal.external_status('unknown')

    def test_native_delivery_uses_own_target_and_no_generation_or_style_changes(self):
        accepted=self.enqueue();factory=Mock(return_value=Mock())
        self.delivery.send(factory)
        kind,args=factory.call_args.args
        self.assertEqual(kind,'native_send');self.assertEqual(args[2],payload()['text'])
        self.assertEqual(args[3:5],('filehelper',payload()['request_id']))
        self.store.last_local_id.assert_called_once_with('filehelper')
        self.assertEqual(self.journal.external_status(payload()['request_id'])['state'],'confirming')

    def test_external_ready_task_never_calls_model_generation(self):
        from wxbridge.app.bot import WeChatDeepSeekBot
        self.enqueue();bot=WeChatDeepSeekBot.__new__(WeChatDeepSeekBot)
        bot.cfg=self.cfg;bot.journal=self.journal;bot.generators={};bot.job_factory=Mock()
        bot._generation_step();bot.job_factory.assert_not_called()

    def test_confirmation_before_worker_result_remains_confirmed(self):
        accepted=self.enqueue();job=Mock();factory=Mock(return_value=job)
        self.delivery.send(factory)
        self.store.messages_after.return_value=[dict(local_id=3,sender='synthetic',type=1,text=payload()['text'])]
        self.delivery.confirm()
        job.poll.return_value=dict(ok=False,ui={'error':'native_worker_failed','operation':{'submitted':False}})
        self.delivery.poll_result()
        self.assertEqual(self.journal.external_status(payload()['request_id'])['state'],'confirmed')
        self.assertEqual(self.journal.task(accepted['task_id'])['chunks'],[])

    def test_confirmation_is_per_target_and_completion_survives_pruning(self):
        accepted=self.enqueue();identifier=accepted['task_id']
        self.journal.intend(identifier,2,100,10)
        row=dict(local_id=3,sender='synthetic',type=1,text=payload()['text'])
        self.store.messages_after.side_effect=lambda chat,*a,**kw:[row] if chat=='filehelper' else []
        self.journal.set('proactive_session',{'synthetic':True})
        with patch('wxbridge.app.delivery.confirmation_updates') as policy:
            self.delivery.confirm(memory=True)
            policy.assert_not_called()
        self.store.messages_after.assert_called_once_with('filehelper',2,limit=100)
        self.assertEqual(self.journal.memory_jobs(),[])
        self.assertEqual(self.journal.get('proactive_session'),{'synthetic':True})
        self.assertIsNone(self.journal.get('last_confirmed_outbound_id'))
        self.assertEqual(self.journal.task(identifier)['chunks'],[])
        self.journal.prune_completed(0)
        self.assertIsNone(self.journal.task(identifier))
        result=self.journal.external_status(payload()['request_id'])
        self.assertEqual(result['state'],'confirmed');self.assertEqual(result['ack_ids'],[3])
        self.assertNotIn(payload()['text'],json.dumps(result,ensure_ascii=False))
        self.assertTrue(self.enqueue()['duplicate'])

    def test_wrong_sender_or_body_does_not_confirm(self):
        accepted=self.enqueue();self.journal.intend(accepted['task_id'],2,100,10)
        self.store.messages_after.return_value=[dict(local_id=3,sender='other',type=1,text=payload()['text']),
            dict(local_id=4,sender='synthetic',type=1,text='另一个合成正文'),
            dict(local_id=2,sender='synthetic',type=1,text=payload()['text'])]
        self.delivery.confirm()
        self.assertIsNotNone(self.journal.task(accepted['task_id'])['intent'])

    def test_known_pre_submit_failure_finishes_without_blocking_following_jobs(self):
        accepted=self.enqueue();self.journal.intend(accepted['task_id'],2,100,10)
        job=Mock();job.poll.return_value=dict(ok=False,ui={'error':'native_no_unique_context','operation':{'submitted':False}})
        self.delivery.job=(accepted['task_id'],0,job);self.delivery.poll_result()
        self.assertIsNone(self.journal.head())
        result=self.journal.external_status(payload()['request_id'])
        self.assertEqual(result['state'],'failed');self.assertEqual(result['error'],'native_no_unique_context')
        self.assertTrue(self.enqueue()['duplicate'])

    def test_uncertain_submission_is_persisted_and_never_retried_after_restart(self):
        accepted=self.enqueue();self.journal.intend(accepted['task_id'],2,100,10)
        job=Mock();job.poll.return_value=dict(ok=False,ui={'error':'native_helper_timeout','operation':{'submitted':None}})
        self.delivery.job=(accepted['task_id'],0,job)
        with patch('wxbridge.app.delivery.time.time',return_value=110):self.delivery.poll_result()
        self.journal.close();self.journal=self.open();self.journal.recover(120)
        delivery=DeliveryController(self.cfg,self.journal,self.store,'synthetic@chatroom','合成群')
        self.store.messages_after.return_value=[]
        with patch('wxbridge.app.delivery.time.time',return_value=131):delivery.confirm()
        factory=Mock();delivery.send(factory);factory.assert_not_called()
        self.assertEqual(self.journal.external_status(payload()['request_id'])['state'],'blocked')
        self.assertTrue(self.enqueue()['duplicate'])

    def test_main_api_dedupes_before_reading_chat_database(self):
        self.enqueue();api=NativeAPI.__new__(NativeAPI)
        api.bot=Mock(journal=self.journal,store=Mock());api.cfg=copy.deepcopy(DEFAULTS['native_api'])
        self.assertTrue(api.handle(payload())['duplicate']);api.bot.store.last_local_id.assert_not_called()
        with self.assertRaisesRegex(NativeError,'chat_not_allowed'):api.handle(payload(chat='other@chatroom'))


@unittest.skipUnless(os.name=='posix' and hasattr(socket,'SO_PEERCRED'),'Linux Unix socket peer credentials')
class SocketTests(unittest.TestCase):
    def test_real_partial_frames_roundtrip_and_socket_permissions(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            cfg=copy.deepcopy(DEFAULTS);cfg['native_api'].update(enabled=True,socket=str(Path(directory)/'api.sock'))
            bot=Mock(cfg=cfg);bot.journal.external_duplicate.return_value={'state':'queued','duplicate':True}
            with patch('wxbridge.native.api.require_memory_directory'):api=NativeAPI(bot)
            try:
                self.assertEqual(api.path.stat().st_mode&0o777,0o600)
                client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);client.settimeout(1)
                with client:
                    client.connect(str(api.path));api.tick()
                    frame=encode(payload());client.sendall(frame[:2]);api.tick()
                    client.sendall(frame[2:]);api.tick();api.tick()
                    self.assertEqual(receive(client)['result']['state'],'queued')
            finally:api.close()
            self.assertFalse(Path(cfg['native_api']['socket']).exists())


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS);self.cfg['native_api']['enabled']=True;self.cfg['ui']['dry_run']=False

    def test_draft_blocks_before_helper_and_no_body_input(self):
        from wxbridge.native.sender import submit
        sender=Mock(dry_run=False);sender.a11y.available=True;sender.a11y.chat_is_selected.return_value=True
        sender.a11y.composer.return_value=(Mock(),None);sender.a11y.read_text.return_value='合成草稿'
        with patch('wxbridge.native.sender.Sender',return_value=sender),patch('wxbridge.native.sender.request') as ipc:
            with self.assertRaisesRegex(Exception,'native preparation rejected'):
                submit(self.cfg,'File Transfer','合成文字','filehelper','synthetic')
            ipc.assert_not_called();sender.a11y.prepare_text.assert_not_called();sender.send.assert_not_called()

    def test_ipc_failure_is_uncertain_and_does_not_fall_back(self):
        from wxbridge.native.sender import submit
        sender=Mock(dry_run=False);sender.a11y.available=True;sender.a11y.chat_is_selected.return_value=True
        sender.a11y.composer.return_value=(Mock(),None);sender.a11y.read_text.return_value=''
        with patch('wxbridge.native.sender.Sender',return_value=sender),patch('wxbridge.native.sender.request',side_effect=NativeError('native_service_unavailable')):
            try:submit(self.cfg,'File Transfer','合成文字','filehelper','synthetic')
            except Exception as exc:self.assertIsNone(exc.metadata['submitted'])
            else:self.fail('should refuse uncertain submission')
            sender.send.assert_not_called()


class HelperTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
        self.assets=Path(self.temp.name)
        (self.assets/'profile.json').write_text(json.dumps({'sha256':'synthetic'}))
        (self.assets/'agent.js').write_text('// synthetic, never evaluated')
        self.cfg=dict(uid=995,assets=str(self.assets),allowed_chat_ids=['filehelper'])
        self.exports=Mock()
        self.exports.discover.return_value={'viewFound':True,'scannedMiB':0}
        self.exports.arm.return_value={'accepted':True}
        self.exports.status.return_value={'stage':'returned_unconfirmed','calls':1}
        self.exports.cancel.return_value={'calls':1,'cancelled':True}
        self.script=Mock(exports_sync=self.exports)
        self.session=Mock();self.session.create_script.return_value=self.script
        self.device=Mock();self.device.attach.return_value=self.session
        self.frida=types.SimpleNamespace(get_local_device=lambda:self.device)
        patcher=patch.dict(sys.modules,frida=self.frida);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch('wxbridge.native.helper.find_client',return_value=123)
        self.find=patcher.start();self.addCleanup(patcher.stop)

    def test_unicode_and_script_like_text_are_json_data_and_only_one_call_is_armed(self):
        text='中文😀\n"; throw new Error("synthetic"); //\u2028[偷笑]'
        result=helper.worker(self.cfg,payload(text=text))
        source=self.session.create_script.call_args.args[0]
        binding=json.loads(source.splitlines()[1].removeprefix('const CONFIG=').removesuffix(';'))
        self.assertEqual(binding['text'],text)
        self.assertEqual(binding['byte_length'],len(text.encode()))
        self.assertTrue(result['ok']);self.assertIs(result['ui']['operation']['submitted'],True)
        self.exports.arm.assert_called_once_with();self.exports.cancel.assert_called_once()
        self.session.detach.assert_called_once()
        self.assertNotIn(text,json.dumps(result,ensure_ascii=False))

    def test_profile_or_owner_mismatch_never_attaches(self):
        self.find.side_effect=ValueError('native_profile_mismatch')
        result=helper.worker(self.cfg,payload())
        self.device.attach.assert_not_called()
        self.assertFalse(result['ui']['operation']['submitted'])

    def test_ambiguous_context_does_not_schedule(self):
        self.exports.discover.return_value={'viewFound':False}
        result=helper.worker(self.cfg,payload())
        self.assertEqual(result['ui']['error'],'native_no_unique_context')
        self.exports.arm.assert_not_called()

    def test_rpc_arm_failure_is_uncertain_when_cancellation_cannot_confirm_no_call(self):
        self.exports.arm.side_effect=RuntimeError('synthetic_transport_error')
        self.exports.cancel.side_effect=RuntimeError('synthetic_transport_error')
        result=helper.worker(self.cfg,payload())
        self.assertIsNone(result['ui']['operation']['submitted'])
        self.session.detach.assert_called_once()

    def test_cancellation_proving_zero_calls_is_a_definite_pre_submit_failure(self):
        self.exports.arm.side_effect=RuntimeError('synthetic_transport_error')
        self.exports.cancel.return_value={'calls':0,'cancelled':True}
        result=helper.worker(self.cfg,payload())
        self.assertIs(result['ui']['operation']['submitted'],False)

    def test_parent_timeout_never_claims_not_submitted(self):
        import subprocess
        with patch('wxbridge.native.helper.subprocess.run',side_effect=subprocess.TimeoutExpired('synthetic',10)):
            result=helper.run_child('/synthetic/config.json',payload(),10)
        self.assertIsNone(result['ui']['operation']['submitted'])
        self.assertEqual(result['ui']['error'],'native_helper_timeout')

    def test_agent_errors_are_redacted_and_not_armed(self):
        def hook(name,callback):
            self.script.load.side_effect=lambda:callback({'type':'error','description':'synthetic-private-body'},None)
        self.script.on.side_effect=hook
        result=helper.worker(self.cfg,payload())
        self.assertEqual(result['ui']['error'],'native_agent_error')
        self.assertNotIn('synthetic-private-body',json.dumps(result))
        self.exports.arm.assert_not_called()


if __name__=='__main__':unittest.main()
