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


class ChatSelectionTests(unittest.TestCase):
    def test_label_must_match_first_line_exactly(self):
        self.assertTrue(chat_label_matches('测试群\n[2] 新消息', '测试群'))
        for label in ('测试群2\n消息', '别的群\n测试群', '测试群副本'):
            self.assertFalse(chat_label_matches(label, '测试群'))

    def test_duplicate_titles_and_focus_do_not_confirm_selection(self):
        a = A11y.__new__(A11y)
        a.cfg = {}
        a.Atspi = Mock()
        a.require_app = Mock()
        root = Mock()
        a.find_list = Mock(return_value=root)
        child = Mock()
        child.get_name.return_value = '测试群\n预览'
        child.get_state_set.return_value.contains.side_effect = (
            lambda state: state == a.Atspi.StateType.FOCUSED)
        root.get_child_count.return_value = 1
        root.get_child_at_index.return_value = child
        self.assertIs(a.chat_is_selected('测试群'), False)
        child.get_state_set.return_value.contains.side_effect = (
            lambda state: state == a.Atspi.StateType.SELECTED)
        self.assertIs(a.chat_is_selected('测试群'), True)
        root.get_child_count.return_value = 2
        self.assertIsNone(a.chat_is_selected('测试群'))


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.a = A11y.__new__(A11y)
        self.a.cfg = {}
        self.a.Atspi = Mock()
        self.a.require_app = Mock()

    def editable(self):
        node = Mock()
        node.get_state_set.return_value.contains.side_effect = (
            lambda state: state in (self.a.Atspi.StateType.EDITABLE, self.a.Atspi.StateType.VISIBLE))
        return node

    def test_gi_text_interface_collision_does_not_use_widget_name(self):
        node = Mock()
        interface = node.get_text_iface.return_value
        interface.get_text.side_effect = TypeError('Accessible.get_text takes no arguments')
        node.get_name.return_value = '输入消息'
        self.a.Atspi.Text.get_character_count.return_value = 0
        self.assertEqual(self.a.read_text(node), '')
        self.a.Atspi.Text.get_character_count.return_value = 4
        self.a.Atspi.Text.get_text.return_value = '完整中文'
        self.assertEqual(self.a.read_text(node), '完整中文')

    def test_nested_editor_chosen_instead_of_search_field(self):
        search, editor, send = self.editable(), self.editable(), Mock()
        self.a.walk = Mock(return_value=[
            (search, 'text', '', dict(x=299, y=126, width=126, height=22)),
            (editor, 'text', '', dict(x=521, y=588, width=542, height=79)),
            (send, 'button', 'Send(S)', dict(x=1004, y=677, width=55, height=24)),
        ])
        self.assertEqual(self.a.composer_controls(), (editor, send))

    def test_readback_mismatch_never_clicks_send(self):
        node, button = Mock(), Mock()
        self.a.composer = Mock(return_value=(node, {}))
        self.a.composer_controls = Mock(return_value=(node, button))
        self.a.focus_editable = Mock()
        self.a.type_into = Mock(return_value='xtype')
        self.a.read_text = Mock(side_effect=['', 'garbled'])
        self.a.click_node = Mock()
        with patch('wxbridge.wechat.ui.X11'), patch('wxbridge.wechat.ui.time.sleep'):
            with self.assertRaises(UIError):
                self.a.send_text('完整中文', expected_chat='合成群')
        self.a.click_node.assert_not_called()

    def test_existing_draft_is_preserved(self):
        self.a.composer = Mock(return_value=(Mock(), {}))
        self.a.read_text = Mock(return_value='用户未发出的草稿')
        self.a.type_into = Mock()
        with patch('wxbridge.wechat.ui.X11'):
            with self.assertRaises(UIError):
                self.a.prepare_text('新的回复')
        self.a.type_into.assert_not_called()

    def prepared(self):
        node, button = Mock(), Mock()
        self.a.prepare_text = Mock(return_value="atspi")
        self.a.chat_is_selected = Mock(return_value=True)
        self.a.composer_controls = Mock(return_value=(node, button))
        self.a.focus_metadata = Mock(return_value={"active_native": True})
        self.a.submit_button = Mock(return_value="atspi-action")
        return node, button

    def test_post_submit_gi_exception_is_not_swallowed_or_retried(self):
        self.prepared()
        self.a.read_text = Mock(side_effect=["完整中文", UIError("GI Text 失败")])
        with patch("wxbridge.wechat.ui.time.sleep"), patch("wxbridge.wechat.ui.X11") as x:
            with self.assertRaises(UIError):
                self.a.send_text("完整中文", "合成群")
            x.return_value.key.assert_not_called()
        self.a.submit_button.assert_called_once()

    def test_uncleared_draft_is_observable_metadata_without_resubmit(self):
        self.prepared()
        self.a.read_text = Mock(return_value="完整中文")
        with patch("wxbridge.wechat.ui.time.sleep"):
            result = self.a.send_text("完整中文", "合成群")
        self.assertEqual(result["after_chars"], 4)
        self.a.submit_button.assert_called_once()

    def test_send_prefers_atspi_action_and_failure_does_not_click_again(self):
        button = Mock()
        action = button.get_action_iface.return_value
        action.get_n_actions.return_value = 1
        self.a.Atspi.Action.get_action_name.return_value = "click"
        action.do_action.return_value = True
        with patch("wxbridge.wechat.ui.X11") as x:
            self.assertEqual(self.a.submit_button(button), "atspi-action")
            x.return_value.click.assert_not_called()
            action.do_action.side_effect = RuntimeError("unknown")
            with self.assertRaises(UIError):
                self.a.submit_button(button)
            x.return_value.checked_click.assert_not_called()

    def test_focus_only_action_uses_verified_click_instead_of_do_action(self):
        button = Mock()
        action = button.get_action_iface.return_value
        action.get_n_actions.return_value = 1
        self.a.Atspi.Action.get_action_name.return_value = "SetFocus"
        bounds = dict(x=10, y=20, width=30, height=40)
        self.a._bounds = Mock(return_value=bounds)
        with patch("wxbridge.wechat.ui.X11") as x:
            x.return_value.checked_click.return_value = {"hit_native": True}
            self.assertEqual(self.a.submit_button(button), "x11-click")
            x.return_value.checked_click.assert_called_once_with(bounds)
        action.do_action.assert_not_called()

    def test_real_click_action_selected_by_name_instead_of_first_index(self):
        button = Mock()
        action = button.get_action_iface.return_value
        action.get_n_actions.return_value = 2
        self.a.Atspi.Action.get_action_name.side_effect = lambda interface, i: ["SetFocus", "click"][i]
        action.do_action.return_value = True
        self.assertEqual(self.a.submit_button(button), "atspi-action")
        action.do_action.assert_called_once_with(1)

    def test_authorized_cleanup_only_matches_selected_chat_and_41_chars(self):
        self.a.chat_is_selected = Mock(return_value=True)
        self.a.composer = Mock(return_value=(Mock(), {}))
        self.a.read_text = Mock(side_effect=["字"*41, ""])
        self.a._atspi_write = Mock(return_value=True)
        self.a.focus_editable = Mock()
        with patch("wxbridge.wechat.ui.X11"):
            self.assertEqual(self.a.clear_authorized_draft("合成群", 41),
                             {"before_chars": 41, "after_chars": 0})
        self.a.read_text = Mock(return_value="新草稿")
        self.a._atspi_write.reset_mock()
        with self.assertRaises(UIError):
            self.a.clear_authorized_draft("合成群", 41)
        self.a._atspi_write.assert_not_called()

    def test_configured_input_method_reaches_composer(self):
        self.a.cfg["input_method"] = "xtype"
        node = Mock()
        self.a.composer = Mock(return_value=(node, {}))
        self.a.read_text = Mock(side_effect=["", "文字"])
        self.a.type_into = Mock(return_value="xtype")
        self.a.focus_editable = Mock()
        with patch("wxbridge.wechat.ui.X11"), patch("wxbridge.wechat.ui.time.sleep"):
            self.a.prepare_text("文字")
        self.a.type_into.assert_called_once_with(node, "文字", "xtype")


class NativeWindowTests(unittest.TestCase):
    def test_obstructed_button_never_receives_click(self):
        x = X11.__new__(X11)
        x.activate = Mock(return_value="native-main")
        x.pointer_client = Mock(return_value="app-ex")
        x._is_native_window = Mock(return_value=False)
        x.move_pointer = Mock()
        x._xdotool = Mock()
        with self.assertRaises(UIError) as caught:
            x.checked_click(dict(x=0, y=0, width=10, height=10))
        self.assertEqual(caught.exception.code, "button_obstructed")
        self.assertFalse(any(call.args[0] == "click" for call in x._xdotool.call_args_list))

    def test_verified_native_hit_clicks_once(self):
        x = X11.__new__(X11)
        x.activate = Mock(return_value="native-main")
        x.pointer_client = Mock(return_value="native-main")
        x._is_native_window = Mock(return_value=True)
        x.move_pointer = Mock()
        x._xdotool = Mock()
        self.assertTrue(x.checked_click(dict(x=0, y=0, width=10, height=10))["hit_native"])
        x._xdotool.assert_any_call("click", "1")

    def test_stationary_pointer_never_uses_sync_and_checks_actual_position(self):
        x = X11.__new__(X11)
        x._xdotool = Mock(return_value=Mock(stdout=b"X=10\nY=20\nWINDOW=123\n"))
        x.move_pointer(10, 20)
        x.move_pointer(10, 20)
        self.assertTrue(all("--sync" not in call.args for call in x._xdotool.call_args_list))
        x._xdotool.return_value.stdout = b"X=11\nY=20\n"
        with self.assertRaises(UIError):
            x.move_pointer(10, 20)

    def test_failure_before_click_is_distinguished_from_uncertain_click(self):
        x = X11.__new__(X11)
        x.activate = Mock(return_value="main")
        x.move_pointer = Mock(side_effect=UIError("timeout", code="command_timeout"))
        x._xdotool = Mock()
        with self.assertRaises(UIError) as caught:
            x.checked_click(dict(x=0, y=0, width=2, height=2))
        self.assertIs(caught.exception.metadata["submitted"], False)
        x._xdotool.assert_not_called()
        x.move_pointer.side_effect = None
        x.pointer_client = Mock(return_value="main")
        x._is_native_window = Mock(return_value=True)
        x._xdotool.side_effect = UIError("timeout", code="command_timeout")
        with self.assertRaises(UIError) as caught:
            x.checked_click(dict(x=0, y=0, width=2, height=2))
        self.assertIsNone(caught.exception.metadata["submitted"])
    def test_app_ex_window_is_rejected_even_with_matching_class(self):
        x = X11.__new__(X11)
        x.cfg = {}
        x._xdotool = Mock(return_value=Mock(stdout=b'3150\n'))
        paths = {'/proc/3150/exe': '/opt/wechat/RadiumWMPF/runtime/WeChatAppEx',
                 '/opt/wechat/wechat': '/opt/wechat/wechat'}
        with patch('wxbridge.wechat.ui.os.path.realpath', side_effect=paths.get):
            self.assertFalse(x._is_native_window('browser-window'))
        paths['/proc/3150/exe'] = '/opt/wechat/wechat'
        with patch('wxbridge.wechat.ui.os.path.realpath', side_effect=paths.get):
            self.assertTrue(x._is_native_window('native-window'))

    def test_xdotool_failure_is_not_treated_as_success(self):
        x = X11.__new__(X11)
        x.available = True
        with patch('wxbridge.wechat.ui._run', return_value=Mock(returncode=1)):
            with self.assertRaises(UIError):
                x._xdotool('click', '1')


if __name__ == "__main__":
    unittest.main()
