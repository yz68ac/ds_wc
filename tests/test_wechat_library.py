"""Independent adapter boundaries using synthetic data only."""
import ast
from dataclasses import fields
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_linux import DeliveryState, Message, OwnerBusyError, SendReceipt, SessionConfig, WeChatSession
from wechat_linux.storage.store import WeChatStore
from wxbridge.wechat.messages import display_message

ROOT = Path(__file__).resolve().parents[1]
CHAT = 'synthetic@chatroom'


class BoundaryTests(unittest.TestCase):
    def test_sdk_import_does_not_load_application_or_desktop(self):
        code = """
import sys
import wechat_linux
from wechat_linux import SessionConfig, WeChatSession, NativeSender, capabilities
assert not any(n.startswith(('wxbridge', 'provider', 'frida', 'gi')) for n in sys.modules)
assert not hasattr(wechat_linux, 'NativeClient')
assert capabilities()['native_profiles'][0]['version'] == '4.1.13.26'
assert 'wechat_linux.desktop.ui' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_adapter_has_no_application_imports(self):
        for path in (ROOT/'src/wechat_linux').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                           else [name.name for name in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse(any(name.split('.')[0] in ('wxbridge', 'provider') for name in modules), str(path))

    def test_configuration_contains_only_account_resources(self):
        self.assertEqual({field.name for field in fields(SessionConfig)},
                         {'db_dir', 'keys_file', 'decrypted_dir', 'expected_account'})

    def test_records_keep_raw_types_and_copy_nested_data(self):
        raw = dict(local_id=2, server_id=123, sender='wxid_person', sender_name='昵称', type=49,
                   raw_type=(57 << 32) | 49, message_kind='quoted_text', text='synthetic-private',
                   reference={'text': 'synthetic-quote'}, mentioned_users=None)
        message = Message.from_mapping(CHAT, raw)
        self.assertEqual(message.type, 49)
        self.assertEqual(message.raw_type, raw['raw_type'])
        self.assertEqual(message.message_kind, 'quoted_text')
        self.assertEqual(message.server_id, '123')
        self.assertIsNone(message.mentioned_users)
        self.assertNotIn('synthetic-private', repr(message))
        self.assertNotIn('synthetic-quote', repr(message))
        raw['reference']['text'] = 'changed'
        self.assertEqual(message.reference['text'], 'synthetic-quote')

    def test_bot_presentation_does_not_mutate_adapter_records(self):
        store = WeChatStore.__new__(WeChatStore)
        xml = '<msg><appmsg><type>57</type><title>question</title></appmsg></msg>'
        raw = store._parse_message(CHAT, dict(local_id=1, local_type=(57 << 32) | 49,
                                  create_time=1, message_content=xml), {}, {})
        self.assertEqual(raw['type'], 49)
        self.assertEqual(raw['reference']['reason'], 'reference_missing')
        rendered = display_message(raw)
        self.assertEqual(rendered['type'], 1)
        self.assertEqual(raw['type'], 49)
        self.assertEqual(raw['reference']['reason'], 'reference_missing')
        image = store._parse_message(CHAT, dict(local_id=2, local_type=3, create_time=1,
                                    message_content='<msg><img md5="'+'a'*32+'"/></msg>'), {}, {})
        self.assertEqual(image['text'], '')
        self.assertEqual(display_message(image)['text'], '[图片]')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SessionConfig('/synthetic/db', '/synthetic/keys', '/synthetic/cache', 'wxid_synthetic')
        self.store = Mock(self_wxid='wxid_synthetic')
        self.store.messages_after.return_value = [dict(local_id=4, sender='wxid_person', text='synthetic')]
        self.factory = patch('wechat_linux.session.WeChatStore', return_value=self.store).start()
        self.addCleanup(patch.stopall)

    def test_cursor_is_caller_owned_and_chat_is_explicit(self):
        with WeChatSession(self.cfg) as session:
            batch = session.read_messages(CHAT, after=3)
            self.assertEqual(batch.cursor, 4)
            session.read_messages('another@chatroom', after=3)
            self.store.messages_after.assert_called_with('another@chatroom', 3, limit=100)
            for cursor in (-1, True, '3'):
                with self.assertRaises(ValueError):
                    session.read_messages(CHAT, after=cursor)
            with self.assertRaises(ValueError):
                session.read_messages('', after=0)
        self.assertIsNone(session.store)

    def test_account_mismatch_releases_owned_state(self):
        self.store.self_wxid = 'wxid_wrong'
        session = WeChatSession(self.cfg)
        with self.assertRaisesRegex(Exception, 'account_mismatch'):
            session.__enter__()
        self.assertIsNone(session.store)

    def test_sessions_are_independent_and_do_not_claim_listener_ownership(self):
        with WeChatSession(self.cfg) as first, WeChatSession(self.cfg) as second:
            first.read_messages(CHAT, after=0)
            second.read_messages(CHAT, after=0)
        self.assertEqual(self.factory.call_count, 2)

    def test_sender_is_injected_and_remains_caller_owned(self):
        sender = Mock()
        sender.submit_text.return_value = SendReceipt('request:one', CHAT, DeliveryState.SUBMITTED)
        downloader = Mock()
        with WeChatSession(self.cfg, sender=sender, image_downloader=downloader) as session:
            receipt = session.send_text(CHAT, 'synthetic', request_id='request:one')
            self.assertEqual(receipt.state, DeliveryState.SUBMITTED)
        sender.submit_text.assert_called_once_with(CHAT, 'synthetic', request_id='request:one')
        sender.close.assert_not_called()
        downloader.close.assert_not_called()

    def test_local_image_uses_explicit_limits(self):
        self.store.image_binding.return_value = {'account_root': '/synthetic'}
        with WeChatSession(self.cfg) as session, \
             patch('wechat_linux.media.images.read_local_image', return_value=(b'png', {'quality': 'cached'})) as read:
            self.assertEqual(session.fetch_image(CHAT, {}, max_bytes=123).data, b'png')
        read.assert_called_once_with(self.store.image_binding.return_value, max_bytes=123)


@unittest.skipUnless(sys.platform == 'linux', 'Linux filesystem locks')
class ResourceLockTests(unittest.TestCase):
    def test_lock_is_private_and_applies_only_to_selected_resource(self):
        from wechat_linux.runtime.locking import file_lock
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory)/'first.lock', Path(directory)/'second.lock'
            with file_lock(first), file_lock(second):
                with self.assertRaises(OwnerBusyError):
                    with file_lock(first):
                        pass
            with file_lock(first):
                pass
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
