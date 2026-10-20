"""使用合成数据的离线测试，不联网、不发送微信消息。"""
from __future__ import annotations

import unittest
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.ai.context import ContextError, message_time, select_context
from wxbridge.ai.prompts import build_prompt
from wxbridge.wechat.messages import is_triggered
from synthetic import NOW, msg


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _deep_merge(DEFAULTS, {})
        self.trigger = msg(50, NOW, "@DeepSeek 请总结")

    def test_exact_inclusive_bounds_and_sender_order(self):
        records = [msg(1, NOW - 601, "过期"), msg(4, NOW + 1, "未来"),
                   msg(3, NOW, "同秒群友", "B"), msg(2, NOW - 600, "边界", "A"),
                   self.trigger, msg(5, None, "未知时间")]
        selected = select_context(self.cfg, records, self.trigger)
        self.assertEqual([m["text"] for m in selected], ["边界", "同秒群友"])

    def test_unknown_observation_timestamp_never_used(self):
        parsed = {"time": None, "time_source": "unknown", "text": "合成未知时间消息"}
        self.assertIsNone(message_time(parsed))
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [], parsed, "合成群")
        for value in [None, True, -1, 0, "1700000600", 1700000600.0]:
            self.assertIsNone(message_time({"time": value}))
        self.assertIsNone(message_time({"time": NOW, "time_source": "observed"}))

    def test_no_last_20_limit_or_old_future_leak(self):
        records = [msg(i, NOW - 100 + i, f"记录{i}") for i in range(1, 41)]
        records += [msg(98, NOW - 601, "EXPIRED_SECRET"),
                    msg(99, NOW + 1, "FUTURE_SECRET"), self.trigger]
        prompt = build_prompt(self.cfg, records, self.trigger, "合成群")[1]["content"]
        self.assertIn("记录1", prompt)
        self.assertIn("记录40", prompt)
        self.assertNotIn("EXPIRED_SECRET", prompt)
        self.assertNotIn("FUTURE_SECRET", prompt)
        self.assertEqual(prompt.count(self.trigger["text"]), 1)

    def test_budget_exceeded_cancels_instead_of_partial_history(self):
        self.cfg["target"]["context_max_chars"] = 100
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [msg(1, NOW - 1, "x" * 300)], self.trigger, "合成群")

    def test_non_text_messages_excluded(self):
        image = {**msg(1, NOW - 1, "图片占位"), "type": 3}
        self.assertEqual(select_context(self.cfg, [image], self.trigger), [])

    def test_only_configured_complete_mention_triggers(self):
        self.assertTrue(is_triggered(self.cfg, '@DeepSeek\u2005你好'))
        for text in ('@DeepSeek123 你好', '@DeepSeeker 你好', '小d 你好', '随便聊聊'):
            self.assertFalse(is_triggered(self.cfg, text))

    def test_messages_after_trigger_in_same_second_are_excluded(self):
        records = [msg(49, NOW, '同秒较早消息'), msg(51, NOW, '同秒之后消息')]
        chosen = select_context(self.cfg, records, self.trigger)
        self.assertEqual([m['local_id'] for m in chosen], [49])


if __name__ == "__main__":
    unittest.main()
