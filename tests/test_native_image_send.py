"""Image submission contracts and source binding; no live client or sends."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_linux import DeliveryState, NativeSender, SessionConfig, WeChatSession
from wechat_linux.errors import NativeError, SendError
from wechat_linux.native import helper
from wechat_linux.native.protocol import MAX_IMAGE_BYTES, validate_send_image


CHAT = 'synthetic@chatroom'


def payload():
    return dict(op='send_image', request_id='image:one', chat_id=CHAT,
                image_path='/synthetic/picture.png', image_bytes=100,
                image_sha256='a'*64, image_mime='image/png')


def response(value):
    return dict(ok=True, ui=dict(backend='native_service', operation=dict(submitted=True),
                recipient_verified=True, message_type=3, image_bytes=value['image_bytes'],
                image_mime=value['image_mime'], image_sha256=value['image_sha256']))


class ImageContractTests(unittest.TestCase):
    def test_client_and_helper_accept_the_same_bounded_shape(self):
        value = payload()
        self.assertEqual(validate_send_image(value), value)
        self.assertEqual(helper.validate(value, [CHAT]), 100)
        for field, bad in (('image_path', '../../private'), ('image_path', '/a/../b.png'),
                           ('image_bytes', True), ('image_bytes', MAX_IMAGE_BYTES+1),
                           ('image_sha256', 'bad'), ('image_mime', 'image/svg+xml')):
            candidate = dict(value, **{field: bad})
            with self.subTest(field=field), self.assertRaises(NativeError):
                validate_send_image(candidate)
            with self.subTest(helper=field), self.assertRaises(ValueError):
                helper.validate(candidate, [CHAT])
        with self.assertRaises(ValueError):
            helper.validate(value, ['other@chatroom'])

    def test_explicit_target_digest_and_source_metadata_reach_the_helper(self):
        value = payload()
        sender = NativeSender('/run/synthetic/helper.sock')
        with patch('wechat_linux.native.image_send.image_request', return_value=value), \
             patch('wechat_linux.native.sender.request', return_value=response(value)) as ipc:
            receipt = sender.submit_image(CHAT, '/synthetic/picture.png', request_id='image:one')
        self.assertEqual(receipt.state, DeliveryState.SUBMITTED)
        self.assertEqual(receipt.metadata['image_sha256'], value['image_sha256'])
        self.assertNotIn('image_path', receipt.metadata)
        self.assertEqual(ipc.call_args.args[1], value)
        self.assertEqual(ipc.call_args.kwargs['peer_uid'], 0)

    def test_wrong_image_result_is_uncertain_and_never_retried(self):
        value = payload()
        reply = response(value)
        reply['ui']['image_sha256'] = 'b'*64
        with patch('wechat_linux.native.image_send.image_request', return_value=value), \
             patch('wechat_linux.native.sender.request', return_value=reply) as ipc:
            with self.assertRaises(SendError) as failure:
                NativeSender('/run/synthetic/helper.sock').submit_image(CHAT, '/image.png', request_id='image:one')
        self.assertIsNone(failure.exception.submitted)
        ipc.assert_called_once()

    def test_session_uses_the_same_injected_sender(self):
        sender = Mock()
        session = WeChatSession(SessionConfig('/db', '/keys', '/cache'), sender=sender)
        session.store = Mock()
        session.send_image(CHAT, '/image.png', request_id='image:one')
        sender.submit_image.assert_called_once_with(CHAT, '/image.png', request_id='image:one')


@unittest.skipUnless(os.name == 'posix', 'Local paths of the Linux client')
class ImageFileTests(unittest.TestCase):
    def test_sdk_description_and_helper_snapshot_detect_content_changes(self):
        from wechat_linux.native.image_send import image_request
        from test_media import png
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'image.png'
            data = png()
            path.write_bytes(data)
            value = image_request(CHAT, path, 'image:one')
            self.assertEqual(value['image_sha256'], hashlib.sha256(data).hexdigest())
            helper.image_snapshot(value, path.stat().st_uid)
            path.write_bytes(data[:-1]+bytes([data[-1]^1]))
            with self.assertRaisesRegex(ValueError, 'native_image_changed'):
                helper.image_snapshot(value, path.stat().st_uid)

    def test_unsupported_files_fail_before_ipc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'image.png'
            path.write_bytes(b'<svg/>')
            with patch('wechat_linux.native.sender.request') as ipc:
                with self.assertRaises(NativeError):
                    NativeSender('/run/synthetic/helper.sock').submit_image(CHAT, path, request_id='image:one')
                ipc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
