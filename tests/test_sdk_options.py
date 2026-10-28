"""SDK options avoid fake bot queues/config while preserving legacy call boundaries."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from wechat_linux import NativeOptions, NativeSender, SessionConfig, WeChatSession, capabilities
from wechat_linux.errors import NativeError
from wechat_linux.native.sender import submit


class SDKOptionsTests(unittest.TestCase):
    def test_native_sender_uses_helper_options_without_application_config(self):
        sender = NativeSender('/run/synthetic/helper.sock', ['synthetic@chatroom'])
        result = dict(ok=True,ui=dict(backend='native_service',operation=dict(submitted=True),recipient_verified=True))
        with patch('wechat_linux.native.sender.request',return_value=result) as request:
            sender('synthetic@chatroom','synthetic','sdk:one')
        self.assertEqual(sender.options, NativeOptions('/run/synthetic/helper.sock',('synthetic@chatroom',)))
        self.assertEqual(request.call_args.args[0],'/run/synthetic/helper.sock')
        self.assertNotIn('queue',sender.__dict__)
        self.assertNotIn('cfg',sender.__dict__)
        # The compatibility view remains accepted by the old submit API.
        with patch('wechat_linux.native.sender.request',return_value=result):
            submit(sender.cfg,'unused','synthetic','synthetic@chatroom','sdk:two')

    def test_limits_allowlist_and_dry_run_reject_before_ipc(self):
        options=NativeOptions('/run/synthetic/helper.sock',('synthetic@chatroom',),max_text_bytes=3)
        for candidate,chat,text in ((options,'synthetic@chatroom','four'),
                (options,'other@chatroom','one'),(replace(options,dry_run=True),'synthetic@chatroom','one')):
            with patch('wechat_linux.native.sender.request') as request:
                with self.assertRaises(Exception):submit(candidate,'unused',text,chat,'sdk:one')
                request.assert_not_called()
        for path,chats in (('relative.sock',('synthetic@chatroom',)),('/run/x',()),('/run/x',('bad chat',))):
            with self.assertRaises(NativeError):NativeOptions(path,chats)

    def test_image_options_use_explicit_runtime_and_keep_default_path_compatible(self):
        cfg=SessionConfig('/db','/keys','/run/synthetic/decrypted',('synthetic@chatroom',))
        for active,runtime in ((cfg,str(Path(cfg.decrypted_dir).parent)),(replace(cfg,runtime_dir='/run/custom'),'/run/custom')):
            session=WeChatSession(active)
            session.store=Mock()
            session.store.image_binding.return_value=dict(account_root='/synthetic',download=dict(verified=True))
            session.store.resolve_chat.return_value=('synthetic@chatroom','合成群')
            with patch('wechat_linux.media.images.read_local_image',return_value=(b'thumb',dict(quality='thumbnail'))) as read, \
                 patch('wechat_linux.desktop.image_download.improve_image',return_value=(b'full',dict(quality='cached'))) as improve:
                image=session.fetch_image('synthetic@chatroom',{},download=True,max_bytes=123)
            self.assertEqual(image.data,b'full')
            self.assertEqual(read.call_args.kwargs,dict(max_bytes=123))
            self.assertNotIn('queue',improve.call_args.args[1])
            self.assertEqual(improve.call_args.kwargs,dict(runtime_dir=runtime,max_bytes=123))

    def test_capabilities_describe_sdk_without_application_integration_state(self):
        value=capabilities()['native_image_download']
        self.assertNotIn('bot_integrated',value)
        self.assertFalse(value['enabled_by_default'])


if __name__ == '__main__':unittest.main()
