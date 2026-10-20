"""一次性恢复必须证明未提交；不能把真实点击的未知状态重新排队。"""
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, Mock, patch

spec = importlib.util.spec_from_file_location("recover_focus_only", Path(__file__).resolve().parents[1]/"scripts/recover-focus-only.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class FocusRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.task = dict(id=1, version=1, chunks=["合成正文"], blocked_reason="timeout",
                         intent=dict(part=0, baseline=10, ui=dict(action="atspi-action", before_chars=4, after_chars=4)))
        self.bot = Mock()
        self.bot.cfg = {}
        self.bot.journal.db = MagicMock()
        self.bot.journal.head.return_value = self.task
        self.bot.journal.task.return_value = self.task
        self.a = Mock()
        self.a.chat_is_selected.return_value = True
        self.a.composer_controls.return_value = (Mock(), Mock())
        self.a.read_text.return_value = "合成正文"
        self.a.composer_controls.return_value[1].get_action_iface.return_value.get_n_actions.return_value = 1
        self.a.Atspi.Action.get_action_name.return_value = "SetFocus"
        for target, opts in [
            ("load_config", dict(return_value={"queue": {"file": "queue"}, "decrypted_dir": "decode", "ui": {}})),
            ("instance_lock", dict(side_effect=lambda path: nullcontext())),
            ("WeChatDeepSeekBot", dict(return_value=self.bot)),
            ("A11y", dict(return_value=self.a)),
        ]:
            p = patch.object(recovery, target, **opts)
            p.start()
            self.addCleanup(p.stop)
        p = patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def test_focus_only_exact_draft_can_rearm_once(self):
        recovery.main()
        self.assertIsNone(self.task["intent"])
        self.assertEqual(self.task["recovery"]["reason"], "verified_focus_only_no_submission")
        self.bot.journal.put.assert_called_once()
        self.bot._confirm_step.assert_called_once()

    def test_changed_draft_is_preserved_without_queue_reset(self):
        self.a.read_text.return_value = "用户新草稿"
        with self.assertRaises(ValueError):
            recovery.main()
        self.bot.journal.put.assert_not_called()
        self.assertIsNotNone(self.task["intent"])

    def test_real_click_unknown_status_cannot_rearm(self):
        self.task["intent"]["ui"]["action"] = "x11-click"
        with self.assertRaises(ValueError):
            recovery.main()
        self.bot.journal.put.assert_not_called()

    def test_non_focus_action_cannot_rearm(self):
        self.a.Atspi.Action.get_action_name.return_value = "click"
        with self.assertRaises(ValueError):
            recovery.main()
        self.bot.journal.put.assert_not_called()

    def test_already_sent_only_confirms_and_never_prepares_ui(self):
        def confirm():
            self.task["intent"] = None
        self.bot._confirm_step.side_effect = confirm
        recovery.main()
        self.a.composer_controls.assert_not_called()
        self.bot.journal.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
