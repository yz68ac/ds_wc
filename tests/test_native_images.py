"""Synthetic source, IPC, cancellation and packaging tests. Never attach WeChat."""
from copy import deepcopy
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
import importlib.resources
import json
import os
from pathlib import Path
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_linux import ImageDownloadError, NativeError, NativeImageDownloader, SessionConfig, WeChatSession
from wechat_linux.native import image_contract as contract, image_worker
from wechat_linux.native import helper
from wechat_linux.native.image_download import NativeImageDownloader
from wechat_linux.native.protocol import request

ROOT = Path(__file__).resolve().parents[1]
CHAT, ACCOUNT, SENDER, DIGEST = 'synthetic@chatroom', 'wxid_synthetic', 'wxid_person', 'a'*32
THUMB = (b'synthetic-thumbnail', dict(quality='thumbnail', width=10, height=20))
FULL = (b'synthetic-clear-cache', dict(quality='cached', width=100, height=200))
COMPLETE = dict(ok=True, state='native_completed', requested=True, source_verified=True, download_status=[0, 0])
DONE = dict(stage='done', pinned=False, native_inflight=False, callback_references=0,
            cleanup=True, message_verified=True, coroutine_completion_state=1,
            download_call_returned=True, download_future_state=1, download_status=[0, 0], downloads=1)


def value():
    return dict(op='download_image', request_id='image:one', account=ACCOUNT, chat_id=CHAT,
                server_id='18446744073709551615', sender=SENDER, image_md5=DIGEST, timeout_seconds=5)


def binding():
    return dict(account=ACCOUNT, account_root='/synthetic/private', candidates=[],
                download=dict(verified=True, chat_id=CHAT, server_id=value()['server_id'], sender=SENDER, image_md5=DIGEST))


class ContractTests(unittest.TestCase):
    def test_bounded_uint64_and_strict_shape(self):
        self.assertEqual(contract.validate(value()), value())
        for field, bad in [('server_id', '18446744073709551616'), ('server_id', '0'),
                           ('server_id', '01'), ('server_id', 123), ('timeout_seconds', True),
                           ('timeout_seconds', 31), ('sender', 'name\nprivate'), ('image_md5', 'bad')]:
            with self.subTest(field=field, bad=bad):
                v=value();v[field]=bad
                with self.assertRaises(ValueError):contract.validate(v)
        for field in ('script', 'path', 'profile', 'api_key'):
            v=value();v[field]='untrusted-private'
            with self.assertRaises(ValueError):contract.validate(v)

    def test_chat_account_and_source_are_separate(self):
        with self.assertRaisesRegex(ValueError, 'chat_not_allowed'):image_worker.authorize(value(), dict(allowed_chat_ids=['other@chatroom'], image_download=dict(expected_account=ACCOUNT)))
        with self.assertRaisesRegex(ValueError, 'account_mismatch'):image_worker.authorize(value(), dict(allowed_chat_ids=[CHAT], image_download=dict(expected_account='wxid_other')))

    def test_native_completion_is_stricter_than_a_return_or_cdn_task(self):
        self.assertTrue(contract.completed(DONE))
        for key, bad in [('pinned', True), ('callback_references', 1), ('callback_references', False),
                         ('native_inflight', True), ('message_verified', False), ('cleanup', False),
                         ('coroutine_completion_state', 0), ('download_future_state', 2),
                         ('download_status', [False, False]), ('download_status', [0, 5]), ('unconfirmed', True)]:
            with self.subTest(key=key):
                self.assertFalse(contract.completed(dict(DONE, **{key: bad})))


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.client=NativeImageDownloader('/run/synthetic/helper.sock', ACCOUNT)
        self.transport=patch('wechat_linux.native.image_download.request').start();self.addCleanup(patch.stopall)
        self.transport.return_value=deepcopy(COMPLETE)
        self.read=patch('wechat_linux.native.image_download.read_local_image').start()
        self.read.side_effect=[THUMB, FULL]
        self.now=0
        patch('wechat_linux.native.image_download.time.monotonic', side_effect=lambda:self.now).start()
        patch('wechat_linux.native.image_download.time.sleep', side_effect=lambda delay:setattr(self,'now',self.now+delay)).start()

    def download(self, **options):
        return self.client.download(options.pop('binding', binding()), chat_id=CHAT, request_id='image:one', timeout=5, **options)

    def test_native_ack_requires_source_bound_readable_full_cache(self):
        result=self.download()
        self.assertEqual(result.data,FULL[0]);self.assertEqual(result.info['download']['outcome'],'cache_verified')
        call=self.transport.call_args
        self.assertEqual(call.kwargs['peer_uid'],0)
        self.assertEqual(set(call.args[1]),contract.FIELDS)
        self.assertNotIn('/synthetic/private',json.dumps(call.args[1]))
        self.assertNotIn('synthetic-clear-cache',repr(result))

    def test_already_cached_requires_no_helper(self):
        self.read.side_effect=None;self.read.return_value=FULL
        self.assertEqual(self.download().data,FULL[0]);self.transport.assert_not_called()

    def test_unverified_wrong_chat_account_or_source_never_submits(self):
        for change in ('verified','chat','account','digest','sender'):
            b=binding()
            if change=='verified':b['download']['verified']=False
            elif change=='chat':b['download']['chat_id']='other@chatroom'
            elif change=='account':b['account']='wxid_other'
            else:b['download']['image_md5' if change=='digest' else 'sender']='bad!'
            with self.subTest(change=change),self.assertRaises(ImageDownloadError):self.download(binding=b)
        self.transport.assert_not_called()

    def test_same_id_after_uncertain_ipc_queries_status_without_resubmitting(self):
        self.read.side_effect=[THUMB,THUMB,FULL]
        self.transport.side_effect=[NativeError('native_service_unavailable',submitted=None),deepcopy(COMPLETE)]
        with self.assertRaises(ImageDownloadError) as failure:self.download()
        self.assertIsNone(failure.exception.requested)
        self.assertEqual(self.download().data,FULL[0])
        self.assertEqual([c.args[1]['op'] for c in self.transport.call_args_list],['download_image','image_status'])

    def test_conflicting_source_with_same_id_cannot_submit(self):
        self.download();changed=binding();changed['download']['image_md5']='b'*32
        with self.assertRaisesRegex(ImageDownloadError,'request_conflict'):self.download(binding=changed)
        self.transport.assert_called_once()

    def test_native_success_without_full_cache_is_never_reported_as_downloaded(self):
        self.read.side_effect=None;self.read.return_value=THUMB
        with self.assertRaisesRegex(ImageDownloadError,'cache_unverified') as failure:self.download()
        self.assertTrue(failure.exception.requested);self.transport.assert_called_once()

    def test_pending_only_polls_status_and_timeout_has_unknown_outcome(self):
        self.read.side_effect=None;self.read.return_value=THUMB
        self.transport.return_value=dict(ok=True,state='pending',requested=None)
        with self.assertRaisesRegex(ImageDownloadError,'timeout') as failure:self.download()
        self.assertIsNone(failure.exception.requested)
        self.assertEqual(sum(c.args[1]['op']=='download_image' for c in self.transport.call_args_list),1)

    def test_malformed_error_and_old_helper_do_not_leak_or_fall_back(self):
        for response in ({'ok':False,'error':'private-body secret','requested':None},
                         {'ok':True,'ui':{'operation':{'submitted':True}}},
                         dict(COMPLETE,download_status=[False,False])):
            self.client._requests.clear();self.read.side_effect=None;self.read.return_value=THUMB
            self.transport.return_value=response
            with self.assertRaises(ImageDownloadError) as failure:self.download()
            self.assertNotIn('private-body',str(failure.exception))

    def test_root_peer_rejected_before_any_frame_is_sent(self):
        stream=Mock();stream.__enter__=Mock(return_value=stream);stream.__exit__=Mock(return_value=False)
        stream.getsockopt.return_value=struct.pack('3i',1,999,999)
        with patch('wechat_linux.native.protocol.socket.socket',return_value=stream),\
                patch('wechat_linux.native.protocol.socket.SO_PEERCRED',17,create=True),\
                patch('wechat_linux.native.protocol.socket.AF_UNIX',1,create=True):
            with self.assertRaisesRegex(NativeError,'native_peer_rejected'):request('/run/synthetic/sock',value(),peer_uid=0)
        stream.sendall.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_root_worker_drains_after_ambiguous_arm_before_unloading(self):
        events=[]
        script=Mock();script.exports_sync.arm.side_effect=RuntimeError('private RPC detail')
        script.exports_sync.status.return_value=dict(DONE,stage='ready',cleanup=False)
        script.unload.side_effect=lambda:events.append('unload')
        session=Mock();session.create_script.return_value=script
        session.detach.side_effect=lambda:events.append('detach')
        device=Mock();device.attach.return_value=session
        fcntl=SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=Mock())
        def drained(*args,**kwargs):
            events.append('drain');script.unload.assert_not_called()
            return DONE,True
        cfg=dict(uid=995,assets=str(ROOT/'src/wechat_linux/native'),allowed_chat_ids=[CHAT],
                 image_download=dict(expected_account=ACCOUNT,account_root='/synthetic/account',timeout_seconds=5))
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules,fcntl=fcntl,frida=SimpleNamespace(get_local_device=lambda:device)))
            stack.enter_context(patch.object(image_worker.os,'open',return_value=123))
            stack.enter_context(patch.object(image_worker.os,'close'))
            stack.enter_context(patch.object(image_worker.os,'O_NOFOLLOW',0,create=True))
            stack.enter_context(patch.object(image_worker.os,'fstat',return_value=SimpleNamespace(st_mode=0o100600,st_uid=0)))
            stack.enter_context(patch.object(image_worker,'account_matches',return_value=True))
            stack.enter_context(patch.object(image_worker,'process_identity',return_value='same-process'))
            drain=stack.enter_context(patch.object(image_worker,'drive',side_effect=drained))
            result=image_worker.run(cfg,value(),threading.Event(),Mock(return_value=42))
            self.assertEqual(events,['drain','unload','detach']);drain.assert_called_once()
            self.assertIsNone(result['requested']);self.assertNotIn('private RPC detail',json.dumps(result))
            # Load-time profile exceptions expose no RPC exports. They must
            # fail before arm, rather than quarantining a nonexistent callback.
            events.clear();drain.reset_mock();script.exports_sync.arm.reset_mock()
            script.exports_sync.status.side_effect=RuntimeError('private profile exception')
            result=image_worker.run(cfg,value(),threading.Event(),Mock(return_value=42))
            self.assertEqual(result,contract.failure('native_image_failed'))
            script.exports_sync.arm.assert_not_called();drain.assert_not_called()
            self.assertEqual(events,['unload','detach'])

    def test_account_mismatch_stops_before_attach_or_native_query(self):
        cfg=dict(uid=995,assets=str(ROOT/'src/wechat_linux/native'),allowed_chat_ids=[CHAT],
                 image_download=dict(expected_account=ACCOUNT,account_root='/synthetic/account',timeout_seconds=5))
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules,fcntl=SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=Mock())))
            stack.enter_context(patch.object(image_worker.os,'open',return_value=123))
            stack.enter_context(patch.object(image_worker.os,'close'))
            stack.enter_context(patch.object(image_worker.os,'O_NOFOLLOW',0,create=True))
            stack.enter_context(patch.object(image_worker.os,'fstat',return_value=SimpleNamespace(st_mode=0o100600,st_uid=0)))
            stack.enter_context(patch.object(image_worker,'account_matches',return_value=False))
            attach=Mock();stack.enter_context(patch.dict(sys.modules,frida=SimpleNamespace(get_local_device=attach)))
            result=image_worker.run(cfg,value(),threading.Event(),Mock(return_value=42))
        self.assertEqual(result,contract.failure('native_image_account_mismatch'));attach.assert_not_called()

    def test_timeout_cancel_does_not_allow_unload_of_owned_native_callback(self):
        script=Mock();pending=dict(DONE,pinned=True,native_inflight=True,callback_references=1)
        script.exports_sync.status.side_effect=[RuntimeError('private'),pending,dict(pending,native_inflight=False),DONE]
        stop=threading.Event();stop.set()
        result,safe=image_worker.drive(script,deadline=0,stop=stop,alive=Mock(return_value=True),clock=lambda:1,sleep=Mock())
        self.assertTrue(safe);self.assertIs(result,DONE)
        self.assertEqual(script.exports_sync.status.call_count,4)
        script.exports_sync.cancel.assert_called_once();script.unload.assert_not_called()

    def test_client_exit_is_different_from_a_safe_completion(self):
        result,safe=image_worker.drive(Mock(),deadline=0,stop=threading.Event(),alive=lambda:False)
        self.assertEqual(result,{});self.assertFalse(safe)

    def test_safe_error_summary_does_not_forward_native_details(self):
        result=image_worker.summarize(dict(DONE,error='private-body'),True)
        self.assertFalse(result['ok']);self.assertNotIn('private-body',json.dumps(result))
        self.assertEqual(image_worker.summarize(dict(DONE,error='message_binding_mismatch',downloads=0),True),
                         contract.failure('native_image_source_mismatch',False))

    def test_helper_pending_future_is_retained_and_status_never_requeues(self):
        future=Mock();future.result.side_effect=FutureTimeout;future.done.return_value=False
        executor=Mock();executor.submit.return_value=future
        cfg=dict(allowed_chat_ids=[CHAT],image_download=dict(expected_account=ACCOUNT,timeout_seconds=5))
        with patch.object(image_worker,'ThreadPoolExecutor',return_value=executor):
            manager=image_worker.ImageManager(cfg,Mock())
        self.addCleanup(manager.close)
        self.assertEqual(manager.handle(value())['state'],'pending')
        self.assertEqual(manager.handle(value())['state'],'pending')
        self.assertEqual(manager.handle(dict(op='image_status',request_id='image:one'))['state'],'pending')
        self.assertEqual(manager.handle(dict(value(),request_id='image:two'))['error'],'native_image_busy')
        self.assertEqual(manager.handle(dict(value(),image_md5='b'*32))['error'],'native_image_request_conflict')
        future.done.return_value=True;future.result.side_effect=None;future.result.return_value=COMPLETE
        self.assertEqual(manager.handle(dict(op='image_status',request_id='image:one')),COMPLETE)
        executor.submit.assert_called_once();future.cancel.assert_not_called()
        manager.close();executor.shutdown.assert_called_with(wait=True,cancel_futures=False)

    def test_restart_missing_id_is_unknown_and_never_executes(self):
        with patch.object(image_worker,'ThreadPoolExecutor') as executor:
            manager=image_worker.ImageManager(dict(allowed_chat_ids=[CHAT],image_download=dict(expected_account=ACCOUNT)),Mock())
            try:self.assertIsNone(manager.handle(dict(op='image_status',request_id='old:image'))['requested'])
            finally:manager.close()
        executor.return_value.submit.assert_not_called()

    def test_history_full_refuses_to_forget_dedupe(self):
        with patch.object(image_worker,'ThreadPoolExecutor'):
            manager=image_worker.ImageManager(dict(allowed_chat_ids=[CHAT],image_download=dict(expected_account=ACCOUNT)),Mock())
        try:
            manager.history={str(i):None for i in range(4096)}
            self.assertEqual(manager.handle(value())['error'],'native_image_history_full')
        finally:manager.close()

    def test_account_guard_needs_one_exact_active_db_root(self):
        fake=Mock();fake.iterdir.return_value=['fd1','fd2']
        with patch.object(image_worker,'Path',return_value=fake):
            fake.__truediv__=Mock(return_value=fake)
            with patch.object(image_worker.os,'readlink',side_effect=['/synthetic/account/db_storage/message/a.db','socket:[12]']):
                self.assertTrue(image_worker.account_matches(1,'/synthetic/account'))
            with patch.object(image_worker.os,'readlink',side_effect=['/synthetic/account/db_storage/a.db','/other/account/db_storage/a.db']):
                self.assertFalse(image_worker.account_matches(1,'/synthetic/account'))


class SessionAndAssetTests(unittest.TestCase):
    def test_old_helper_configuration_needs_no_image_assets_or_worker(self):
        cfg=dict(socket='/run/synthetic/helper.sock',uid=995,allowed_chat_ids=[CHAT],
                 assets='/synthetic/trusted-assets',worker_timeout_seconds=10)
        config_path=Mock();config_path.read_text.return_value=json.dumps(cfg)
        visited=[]
        def trusted(path):
            visited.append(str(path))
            return config_path if str(path)=='/synthetic/config.json' else Path(path)
        with patch.object(helper,'trusted',side_effect=trusted):
            self.assertEqual(helper.configuration('/synthetic/config.json'),cfg)
        self.assertFalse(any('image_' in path for path in visited))
        for option in (True,{'enabled':False,'script':'untrusted'}, {'enabled':True}):
            config_path.read_text.return_value=json.dumps(dict(cfg,image_download=option))
            with patch.object(helper,'trusted',side_effect=trusted),self.assertRaises(ValueError):
                helper.configuration('/synthetic/config.json')

    @unittest.skipUnless(shutil.which('node'),'Node is optional for the Frida JS simulator')
    def test_real_agent_state_machine_with_synthetic_native_memory(self):
        p=subprocess.run(['node',str(ROOT/'tests/native_image_agent_harness.cjs')],cwd=ROOT,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr.decode())
        self.assertGreaterEqual(json.loads(p.stdout)['scenarios'],8)

    def test_native_backend_is_explicit_and_never_calls_desktop_or_resolves_title(self):
        cfg=SessionConfig('/db', '/keys', '/run/synthetic/decrypted', ACCOUNT)
        downloader=Mock();downloader.download.return_value='native-image'
        session=WeChatSession(cfg,image_downloader=downloader);session.store=Mock(self_wxid=ACCOUNT)
        session.store.image_binding.return_value=binding()
        with patch('wechat_linux.desktop.image_download.improve_image') as desktop:
            self.assertEqual(session.fetch_image(CHAT,{},download=True,request_id='image:one'),'native-image')
        desktop.assert_not_called();session.store.resolve_chat.assert_not_called()
        downloader.download.assert_called_once()

    def test_native_download_not_configured_has_no_automatic_fallback(self):
        session=WeChatSession(SessionConfig('/db', '/keys', '/run/synthetic/decrypted', ACCOUNT));session.store=Mock()
        with self.assertRaisesRegex(Exception,'image_downloader_not_configured'):
            session.fetch_image(CHAT,{},download=True,request_id='image:one')

    def test_new_assets_and_api_import_without_bot_frida_gi_or_env(self):
        code="""
import sys
from wechat_linux import NativeImageDownloader, ImageDownloadError, capabilities
assert not any(n.startswith(('wxbridge','frida','gi')) for n in sys.modules)
assert not any(n.startswith('wechat_linux.desktop') for n in sys.modules)
assert capabilities()['native_image_download']['enabled_by_default'] is False
"""
        p=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=ROOT,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr.decode())
        assets=importlib.resources.files('wechat_linux.native')
        for name in contract.ASSETS:self.assertTrue(assets.joinpath(name).is_file())
        profile=json.loads(assets.joinpath('image_profile.json').read_text(encoding='utf-8'))
        self.assertEqual(profile['backend'],'image_coroutine_v1')
        self.assertEqual(profile['sha256'],'2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132')
