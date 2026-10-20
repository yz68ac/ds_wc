"""完整启动流程的离线回归：不恢复旧触发、保留配置、明确发送开关。"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge.cli import main
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.app.runtime import run_bot


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.cfg = _deep_merge(DEFAULTS, {})
        self.cfg["state_file"] = str(Path(self.temp.name) / "state.json")
        self.cfg["target"]["chat"] = "合成群"
        p = patch("wxbridge.app.runtime.instance_lock", side_effect=lambda path: nullcontext())
        p.start()
        self.addCleanup(p.stop)

    def test_baseline_before_poll_and_no_config_mutation(self):
        self.cfg["reply_to_backlog"] = True
        self.cfg["target"]["trigger"]["reply_all"] = True
        original = copy.deepcopy(self.cfg)
        bot, order = Mock(), Mock()
        order.attach_mock(bot.save_status, "status")
        order.attach_mock(bot.run, "run")
        order.attach_mock(bot.close, "close")
        with patch("wxbridge.app.runtime.setup_logging"), patch("wxbridge.app.runtime.WeChatDeepSeekBot", return_value=bot) as make_bot, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(run_bot(self.cfg), 0)
        self.assertEqual(self.cfg, original)
        self.assertEqual([c[0] for c in order.mock_calls], ["status", "run", "close"])
        active_cfg = make_bot.call_args.args[0]
        self.assertTrue(active_cfg["queue"]["file"].endswith(".dry-run"))
        self.assertFalse(active_cfg["reply_to_backlog"])
        self.assertTrue(active_cfg["target"]["trigger"]["require_mention_metadata"])
        self.assertFalse(active_cfg["target"]["trigger"]["reply_all"])

    def test_status_marks_stopped_even_when_loop_raises(self):
        bot = Mock()
        bot.run.side_effect = RuntimeError("synthetic failure")
        with patch("wxbridge.app.runtime.setup_logging"), patch("wxbridge.app.runtime.WeChatDeepSeekBot", return_value=bot), patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(RuntimeError):
                run_bot(self.cfg)
        bot.close.assert_called_once()

    def test_cli_send_and_dry_run_are_explicit_and_keep_model(self):
        for args, expected in [(["run"], True), (["run", "--dry-run"], True), (["run", "--send"], False)]:
            with self.subTest(args=args), patch("wxbridge.cli.load_config", return_value=copy.deepcopy(self.cfg)), patch("wxbridge.cli.run_bot", return_value=0) as run:
                self.assertEqual(main(args), 0)
                active_cfg = run.call_args.args[0]
                self.assertIs(active_cfg["ui"]["dry_run"], expected)
                self.assertEqual(active_cfg["deepseek"]["model"], "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
