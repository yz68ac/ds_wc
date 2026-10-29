"""Native wire options and explicit dependency injection; no live client."""
from dataclasses import fields
import unittest
from unittest.mock import Mock, patch

from wechat_linux import DeliveryState, NativeOptions, NativeSender, SessionConfig, WeChatSession, capabilities
from wechat_linux.errors import NativeError


class SDKOptionsTests(unittest.TestCase):
    def test_native_sender_needs_only_helper_connection_options(self):
        sender = NativeSender('/run/synthetic/helper.sock')
        result = dict(ok=True, ui=dict(backend='native_service', operation=dict(submitted=True), recipient_verified=True))
        with patch('wechat_linux.native.sender.request', return_value=result) as request:
            receipt = sender.submit_text('synthetic@chatroom', 'synthetic', request_id='sdk:one')
        self.assertEqual(receipt.state, DeliveryState.SUBMITTED)
        self.assertEqual(sender.options, NativeOptions('/run/synthetic/helper.sock'))
        self.assertEqual(request.call_args.kwargs['peer_uid'], 0)
        self.assertFalse(hasattr(sender, 'cfg'))
        self.assertEqual({field.name for field in fields(NativeOptions)},
                         {'helper_socket', 'max_text_bytes', 'timeout_seconds'})

    def test_wire_limits_reject_before_ipc(self):
        sender = NativeSender('/run/synthetic/helper.sock', max_text_bytes=3)
        with patch('wechat_linux.native.sender.request') as request:
            with self.assertRaises(NativeError):
                sender.submit_text('synthetic@chatroom', 'four', request_id='sdk:one')
            request.assert_not_called()
        for options in ({'helper_socket': 'relative.sock'}, {'helper_socket': '/run/x', 'max_text_bytes': 0},
                        {'helper_socket': '/run/x', 'timeout_seconds': float('nan')}):
            with self.assertRaises(NativeError):
                NativeOptions(**options)

    def test_downloader_is_selected_by_injection(self):
        session = WeChatSession(SessionConfig('/db', '/keys', '/cache'), image_downloader=Mock())
        session.store = Mock()
        session.image_downloader.download.return_value = 'image'
        self.assertEqual(session.fetch_image('synthetic@chatroom', {}, download=True, max_bytes=123), 'image')
        self.assertEqual(session.image_downloader.download.call_args.kwargs['max_bytes'], 123)

    def test_capabilities_describe_only_adapter_features(self):
        self.assertFalse(capabilities()['native_image_download']['enabled_by_default'])
        self.assertNotIn('outbound_confirmation', capabilities()['features'])


if __name__ == '__main__':
    unittest.main()
