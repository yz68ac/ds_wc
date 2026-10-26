"""SDK boundaries and failure recovery; synthetic records only, no live client."""
from contextlib import closing, nullcontext
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_linux import DeliveryState, Message, NativeError, OwnerBusyError, SessionConfig, WeChatSession
from wechat_linux.sending.outbox import ReliableSender

ROOT=Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_sdk_import_does_not_load_bot_models_desktop_or_credentials(self):
        code="""
import sys
from wechat_linux import SessionConfig, WeChatSession, NativeClient, NativeSender, capabilities
assert not any(n.startswith(('wxbridge','frida','gi')) for n in sys.modules)
assert capabilities()['native_profiles'][0]['version']=='4.1.13.26'
assert 'wechat_linux.desktop.ui' not in sys.modules
"""
        result=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=ROOT,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_sdk_source_has_no_bot_imports(self):
        for path in (ROOT/'wechat_linux').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                modules=([node.module or ''] if isinstance(node,ast.ImportFrom)
                         else [n.name for n in node.names] if isinstance(node,ast.Import) else [])
                self.assertFalse(any(n=='wxbridge' or n.startswith('wxbridge.') for n in modules),str(path))

    def test_legacy_modules_preserve_class_identity(self):
        for legacy,canonical in [('wxbridge.wechat.store','wechat_linux.storage.store'),
                ('wxbridge.wechat.ui','wechat_linux.desktop.ui'),('wxbridge.native.helper','wechat_linux.native.helper'),
                ('wxbridge.native.protocol','wechat_linux.native.protocol'),('wxbridge.ai.wxgf','wechat_linux.media.wxgf')]:
            self.assertIs(importlib.import_module(legacy),importlib.import_module(canonical))

    def test_records_preserve_id_and_missing_mentions_without_exposing_body(self):
        raw=dict(local_id=2,server_id=123,sender='wxid_synthetic',sender_name='昵称',text='synthetic-private',
                 reference={'text':'synthetic-quote'},mentioned_users=None)
        message=Message.from_mapping('synthetic@chatroom',raw)
        self.assertEqual(message.sender_id,'wxid_synthetic');self.assertEqual(message.server_id,'123')
        self.assertIsNone(message.mentioned_users);self.assertNotIn('synthetic-private',repr(message))
        self.assertNotIn('synthetic-quote',repr(message))
        raw['reference']['text']='changed';self.assertEqual(message.reference['text'],'synthetic-quote')

    def test_relocation_does_not_change_privileged_assets_or_deploy_shim(self):
        deploy=runpy.run_path(str(ROOT/'scripts/deploy-release.py'))
        old={'files':{}};new={'files':{}}
        for name in ('helper.py','agent.js','profile.json'):
            body=(ROOT/'wechat_linux/native'/name).read_bytes()
            # Audited 4.1.13.26 service_v1 baseline, normalized release bytes.
            expected={'helper.py':'e5e8ba3a0d4921e385f36ef48b8efd482c4610f9cadfb5ec0f88f417935e0eec',
                      'agent.js':'213197f9b630a6c7796e531c0aecfecaaaef552adefc312a1f97cf0930757790',
                      'profile.json':'c8b22bb22e05019d4f538bc473b893b6fd280b5a8a7ee23d7bb180a0bcc58c96'}
            self.assertEqual(hashlib.sha256(body.replace(b'\r\n',b'\n')).hexdigest(),expected[name])
            info={'sha256':hashlib.sha256(body).hexdigest()}
            old['files']['wxbridge/native/'+name]=info
            new['files']['wxbridge/native/'+name]={'sha256':'synthetic-shim'}
            new['files']['wechat_linux/native/'+name]=info
        self.assertEqual(deploy['helper_changes'](old,new),[])
        new['files']['wechat_linux/native/agent.js']={'sha256':'changed'}
        self.assertEqual(deploy['helper_changes'](old,new),['agent.js'])
        self.assertEqual(deploy['helper_asset'](new,'helper.py'),'wechat_linux/native/helper.py')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.cfg=SessionConfig('/synthetic/db','/synthetic/keys','/run/synthetic/decrypted',
                               ('synthetic@chatroom',),'wxid_synthetic')
        self.store=Mock(self_wxid='wxid_synthetic')
        self.store.messages_after.return_value=[dict(local_id=4,sender='wxid_person',sender_name='新名',text='synthetic')]
        self.lock=Mock();self.lock.__enter__=Mock();self.lock.__exit__=Mock()
        for name,value in [('instance_lock',self.lock),('WeChatStore',self.store)]:
            p=patch('wechat_linux.session.'+name,return_value=value);p.start();self.addCleanup(p.stop)

    def test_cursor_is_caller_owned_and_chats_are_allowlisted(self):
        with WeChatSession(self.cfg) as session:
            batch=session.read_messages('synthetic@chatroom',after=3)
            self.assertEqual(batch.cursor,4);self.assertEqual(batch.messages[0].sender_id,'wxid_person')
            session.read_messages('synthetic@chatroom',after=3)
            self.assertEqual(self.store.messages_after.call_args.args[1],3)
            with self.assertRaisesRegex(Exception,'chat_not_allowed'):session.read_messages('other@chatroom',after=0)
            for cursor in (-1,True,'3'):
                with self.assertRaises(ValueError):session.read_messages('synthetic@chatroom',after=cursor)
        self.lock.__exit__.assert_called_once()

    def test_mismatch_releases_lock_and_does_not_open_outbox(self):
        self.store.self_wxid='wxid_wrong'
        with self.assertRaisesRegex(Exception,'account_mismatch'):
            with WeChatSession(self.cfg):pass
        self.lock.__exit__.assert_called_once()

    def test_busy_lock_is_not_released_as_if_acquired(self):
        self.lock.__enter__.side_effect=OwnerBusyError('database_owner_busy')
        with self.assertRaises(OwnerBusyError):
            with WeChatSession(self.cfg):pass
        self.lock.__exit__.assert_not_called()

    def test_fetch_keeps_binding_error_and_explicit_backend(self):
        self.store.image_binding.return_value={'account_root':'/synthetic','error':'image_reference_wrong_chat'}
        with WeChatSession(self.cfg) as session:
            with self.assertRaisesRegex(Exception,'image_reference_wrong_chat'):
                session.fetch_image('synthetic@chatroom',{},ui={'backend':'native_service'})


class OutboxTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'.test-tmp';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'sdk.sqlite3'
        self.store=Mock(self_wxid='wxid_synthetic');self.store.last_local_id.return_value=10
        self.store.messages_after.return_value=[]
        self.submit=Mock(return_value={'operation':{'submitted':True}})
        for target,options in [('wechat_linux.sending.outbox.require_memory_directory',{}),
                ('wechat_linux.sending.outbox.instance_lock',{'side_effect':lambda p:nullcontext()})]:
            p=patch(target,**options);p.start();self.addCleanup(p.stop)
        if os.name=='nt':
            actual=os.fstat
            def metadata(fd):
                value=actual(fd)
                return SimpleNamespace(st_mode=0o100600,st_uid=0,st_size=value.st_size)
            for name,options in [('geteuid',dict(return_value=0)),('O_NOFOLLOW',dict(new=0)),('fstat',dict(side_effect=metadata))]:
                p=patch('wechat_linux.sending.outbox.os.'+name,create=True,**options);p.start();self.addCleanup(p.stop)
        self.open()

    def open(self,**options):
        self.sender=ReliableSender(self.path,self.store,self.submit,['synthetic@chatroom'],**options)
        self.addCleanup(self.sender.close)

    def send(self,identifier='sdk:one',text='synthetic-private'):
        return self.sender.send_text('synthetic@chatroom',text,identifier)

    def test_intent_commits_before_submission_and_uncertain_restart_never_resends(self):
        def interrupted(*args):
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(json.loads(db.execute('SELECT data FROM intents').fetchone()[0])['state'],'unknown')
            raise ConnectionError('synthetic-private')
        self.submit.side_effect=interrupted
        first=self.send();self.assertEqual(first.state,DeliveryState.UNKNOWN)
        self.assertNotIn('synthetic-private',repr(first))
        self.sender.close();self.open()
        self.assertEqual(self.send().state,DeliveryState.UNKNOWN);self.assertEqual(self.submit.call_count,1)
        with self.assertRaisesRegex(NativeError,'request_id_conflict'):self.send(text='changed')
        with self.assertRaisesRegex(NativeError,'delivery_busy'):self.send('sdk:two')
        self.sender.confirm('sdk:one');self.assertEqual(self.submit.call_count,1)

    def test_confirmation_requires_new_self_exact_target_body_and_erases_text(self):
        self.send()
        self.store.messages_after.return_value=[dict(local_id=10,sender='wxid_synthetic',type=1,text='synthetic-private'),
            dict(local_id=11,sender='other',type=1,text='synthetic-private'),
            dict(local_id=12,sender='wxid_synthetic',type=1,text='other')]
        self.assertEqual(self.sender.confirm('sdk:one').state,DeliveryState.SUBMITTED)
        self.store.messages_after.return_value=[dict(local_id=13,sender='wxid_synthetic',type=1,text='synthetic-private')]
        receipt=self.sender.confirm('sdk:one')
        self.assertEqual(receipt.state,DeliveryState.CONFIRMED);self.assertEqual(receipt.ack_id,13)
        self.assertEqual(self.sender._row('sdk:one')[1]['text'],'')
        self.assertTrue(all(c.args[0]=='synthetic@chatroom' for c in self.store.messages_after.call_args_list))
        self.sender.close();self.open();self.assertEqual(self.send().state,DeliveryState.CONFIRMED)
        self.submit.assert_called_once()

    def test_late_filled_record_is_found_by_bounded_audit(self):
        with patch('wechat_linux.sending.outbox.time.time',return_value=100):self.send()
        row=self.sender._row('sdk:one')[1];row['scan_cursor']=500
        with self.sender.db:self.sender._put(row)
        self.store.messages_after.side_effect=lambda chat,cursor,**kw:([dict(local_id=11,sender='wxid_synthetic',type=1,text='synthetic-private')]
                                                                    if cursor==10 else [])
        with patch('wechat_linux.sending.outbox.time.time',return_value=120):result=self.sender.confirm('sdk:one')
        self.assertEqual(result.state,DeliveryState.CONFIRMED);self.assertLessEqual(self.store.messages_after.call_count,3)

    def test_timeout_is_unknown_and_does_not_create_new_submission(self):
        with patch('wechat_linux.sending.outbox.time.time',return_value=100):self.send()
        with patch('wechat_linux.sending.outbox.time.time',return_value=111):result=self.sender.confirm('sdk:one')
        self.assertEqual(result.state,DeliveryState.UNKNOWN);self.assertEqual(result.error,'send_confirmation_timeout')
        self.assertEqual(self.send().state,DeliveryState.UNKNOWN);self.submit.assert_called_once()

    def test_definite_rejection_clears_text_and_history_cap_keeps_dedupe(self):
        self.sender.close();self.open(max_history=1)
        self.submit.side_effect=NativeError('native_chat_not_allowed',submitted=False)
        self.assertEqual(self.send().state,DeliveryState.NOT_SUBMITTED)
        self.assertEqual(self.sender._row('sdk:one')[1]['text'],'')
        self.send();self.submit.assert_called_once()
        with self.assertRaisesRegex(NativeError,'request_history_full'):self.send('sdk:two')

    def test_wrong_account_and_foreign_database_are_rejected_without_mutation(self):
        self.sender.close();before=self.path.read_bytes();self.store.self_wxid='wxid_other'
        with self.assertRaisesRegex(NativeError,'outbox_account_mismatch'):self.open()
        self.assertEqual(self.path.read_bytes(),before)
        self.store.self_wxid='wxid_synthetic';self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE important(value TEXT)');db.commit()
        self.path.chmod(0o600);before=self.path.read_bytes()
        with self.assertRaisesRegex(NativeError,'foreign_outbox'):self.open()
        self.assertEqual(self.path.read_bytes(),before)

    def test_allowlist_and_limits_fail_before_submission(self):
        with self.assertRaisesRegex(NativeError,'chat_not_allowed'):
            self.sender.send_text('other@chatroom','synthetic','sdk:one')
        for limit in (True,0,4097):
            with self.assertRaisesRegex(NativeError,'invalid_history_limit'):self.open(max_history=limit)
        self.submit.assert_not_called()


@unittest.skipUnless(sys.platform=='linux','Linux filesystem and flock')
class LinuxBoundaryTests(unittest.TestCase):
    def test_outbox_lock_survives_failed_second_owner_and_permissions_are_strict(self):
        store=Mock(self_wxid='wxid_synthetic')
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'outbox.sqlite3'
            with patch('wechat_linux.sending.outbox.require_memory_directory'),patch('wechat_linux.runtime.locking.require_memory_directory'):
                sender=ReliableSender(target,store,Mock(),['filehelper'])
                try:
                    with self.assertRaises(OwnerBusyError):ReliableSender(target,store,Mock(),['filehelper'])
                    self.assertEqual(target.stat().st_mode&0o777,0o600)
                finally:sender.close()
                target.chmod(0o644)
                with self.assertRaisesRegex(NativeError,'unsafe_outbox'):ReliableSender(target,store,Mock(),['filehelper'])
                link=Path(directory)/'link';link.symlink_to(target)
                with self.assertRaisesRegex(NativeError,'unsafe_outbox'):ReliableSender(link,store,Mock(),['filehelper'])

    def test_sessions_and_bot_share_nonblocking_lock(self):
        from wechat_linux.runtime.locking import instance_lock
        from wxbridge.app.runtime import instance_lock as bot_lock
        self.assertIs(bot_lock,instance_lock)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'lock'
            with patch('wechat_linux.runtime.locking.require_memory_directory'):
                with instance_lock(target):
                    with self.assertRaises(OwnerBusyError):
                        with instance_lock(target):pass
                with instance_lock(target):pass
            self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_generic_socket_server_needs_no_bot_handler(self):
        from wechat_linux.transport.server import NativeServer
        from wechat_linux.native.protocol import encode,receive
        with tempfile.TemporaryDirectory() as directory:
            config=dict(enabled=True,socket=directory+'/api.sock',helper_socket=directory+'/helper.sock',
                        allowed_chat_ids=['filehelper'],max_text_bytes=4096,max_pending=32,max_requests=4096)
            service=Mock();service.handle.return_value={'standalone':True}
            server=NativeServer(config,service,directory_validator=lambda p:None)
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(1);client.connect(config['socket']);server.tick()
                    client.sendall(encode({'op':'info'}));server.tick();server.tick()
                    self.assertEqual(receive(client)['result'],{'standalone':True})
                service.handle.assert_called_once_with({'op':'info'})
            finally:server.close()
