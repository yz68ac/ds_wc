"""使用合成数据的离线测试，不联网、不发送微信消息。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from wxbridge.wechat.ui import A11y, Sender, UIError, X11, chat_label_matches


class SendGuardTests(unittest.TestCase):
    def setUp(self):
        self.sender = Sender.__new__(Sender)
        self.sender.ui_cfg = {"verify_chat_selected": True}
        self.sender.dry_run = False
        self.sender.backend = "atspi"
        self.sender.a11y = Mock()
        self.sender._send_xdotool = Mock()

    def test_unknown_or_wrong_chat_does_not_send_or_fallback(self):
        for state in (False, None):
            with self.subTest(state=state):
                self.sender.a11y.chat_is_selected.return_value = state
                with self.assertRaises(UIError):
                    self.sender.send("合成群", "合成回复")
                self.sender.a11y.send_text.assert_not_called()
                self.sender._send_xdotool.assert_not_called()

    def test_uncertain_send_does_not_retry_and_duplicate(self):
        self.sender.a11y.chat_is_selected.return_value = True
        self.sender.a11y.send_text.side_effect = RuntimeError("模拟结果不明")
        with self.assertRaises(UIError):
            self.sender.send("合成群", "合成回复")
        self.sender.a11y.send_text.assert_called_once()
        self.sender._send_xdotool.assert_not_called()

    def test_draft_failure_category_survives_sender_wrapper(self):
        self.sender.a11y.chat_is_selected.return_value = True
        self.sender.a11y.send_text.side_effect = UIError("不同草稿", code="draft_conflict")
        with self.assertRaises(UIError) as caught:
            self.sender.send("合成群", "合成回复")
        self.assertEqual(caught.exception.code, "draft_conflict")

    def test_verified_chat_can_use_mock_transport(self):
        self.sender.a11y.chat_is_selected.return_value = True
        self.sender.send("合成群", "合成回复")
        self.sender.a11y.send_text.assert_called_once_with("合成回复", expected_chat="合成群")








if __name__ == "__main__":
    unittest.main()
