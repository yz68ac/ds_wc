"""引用解析及 API 提示词：合成正文，不读取实际聊天或调用网络。"""
from __future__ import annotations
from tests.support.configuration import replace_config, update_config, snapshot
from tests.support.model_support import default_config
from contextlib import nullcontext

import html
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock

from wxbridge.config import default_document
from tests.support.configuration import merge_config as _deep_merge
from wxbridge.ai.context.selection import ContextError
from wxbridge.ai.reply.prompt import build_prompt
from wechat_linux.messages.references import parse_quoted_reply
from wxbridge.wechat.store import WeChatStore
from wxbridge.wechat.messages import display_message
from tests.support.synthetic import NOW, msg
from tests.support.messages import PACKED_REFERENCE, quoted_xml, parse_message


class ReferenceParsingTests(unittest.TestCase):





    def test_link_snapshot_has_title_description_url_but_never_claims_full_page(self):
        link = "<msg><appmsg><type>5</type><title>标题</title><des>摘要</des><url>https://example.invalid/a</url></appmsg></msg>"
        reference = parse_message(quoted_xml(content=link, kind=49))["reference"]
        self.assertEqual(reference["kind"], "link")
        self.assertIn("标题\n摘要", reference["text"])
        self.assertIn("未抓取网页正文", reference["reason"])




class ReferencePromptTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

        self.cfg = replace_config(self.cfg, 'system_prompt_file', str(Path(__file__).resolve().parents[1]/'prompts/system.md'), owner=self)

    def test_explicit_old_reference_included_but_unrelated_old_history_excluded(self):
        trigger = parse_message(quoted_xml(content="EXPLICIT_OLD_QUOTE"))
        history = [msg(1, NOW-10000, "UNRELATED_OLD_SECRET"), msg(2, NOW-1, "WINDOW_TEXT"), trigger]
        prompt = build_prompt(self.cfg, history, trigger, "合成群")[1]["content"]
        self.assertIn("EXPLICIT_OLD_QUOTE", prompt)
        self.assertIn("WINDOW_TEXT", prompt)
        self.assertNotIn("UNRELATED_OLD_SECRET", prompt)
        self.assertEqual(prompt.count("@ds 这怎么看"), 1)
        self.assertIn("小明", prompt)
        self.assertIn('"sender":"wxid_original"', prompt)
        self.assertNotIn("1234567890123456789", prompt)
        self.assertNotIn("<refermsg>", prompt)

    def test_unavailable_media_and_nested_quote_have_clear_boundary(self):
        trigger = parse_message(quoted_xml(content="IMAGE_PRIVATE_XML", kind=3))
        prompt = build_prompt(self.cfg, [], trigger, "合成群")
        from wxbridge.ai.reply.output import response_prompt
        rules = response_prompt(prompt)[0]["content"]
        prompt = prompt[1]["content"]
        self.assertIn('"available":false', prompt)
        self.assertIn("不要猜测图片", rules)
        self.assertNotIn("IMAGE_PRIVATE_XML", prompt)

    def test_reference_counts_towards_budget_without_silent_truncation(self):
        self.cfg = replace_config(self.cfg, 'target.context_max_chars', 300, owner=self)
        trigger = parse_message(quoted_xml(content="长"*1000))
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [], trigger, "合成群")

    def test_previous_context_quote_remains_explicitly_bound_to_its_actual_author(self):
        old = parse_message(quoted_xml(content="UNREQUESTED_REFERENCE"))
        old.update(local_id=49, time=NOW-1)
        trigger = msg(50, NOW, "@ds 普通问题")
        prompt = build_prompt(self.cfg, [old], trigger, "合成群")[1]["content"]
        self.assertIn("UNREQUESTED_REFERENCE", prompt)
        self.assertIn('"sender":"wxid_original"', prompt)
        self.assertIn(old["text"], prompt)


if __name__ == "__main__":
    unittest.main()
