"""引用解析及 API 提示词：合成正文，不读取实际聊天或调用网络。"""
from __future__ import annotations
from model_support import default_config
from contextlib import nullcontext

import html
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock

from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.ai.context import ContextError
from wxbridge.ai.prompts import build_prompt
from wxbridge.wechat.references import parse_quoted_reply
from wxbridge.wechat.store import WeChatStore
from wxbridge.wechat.messages import display_message
from synthetic import NOW, msg

PACKED_REFERENCE = (57 << 32) | 49


def quoted_xml(question="@ds 这怎么看", content="明天 10 点开会", kind=1, *, sender="wxid_original", created=NOW-10000):
    return (f'<msg><appmsg><title>{html.escape(question)}</title><type>57</type><refermsg>'
            f'<type>{kind}</type><svrid>1234567890123456789</svrid><fromusr>{sender}</fromusr>'
            f'<chatusr>synthetic@chatroom</chatusr><displayname>小明</displayname>'
            f'<content>{html.escape(content)}</content><createtime>{created}</createtime>'
            f'<msgsource>&lt;msgsource&gt;&lt;atuserlist&gt;wxid_self&lt;/atuserlist&gt;&lt;/msgsource&gt;</msgsource>'
            '</refermsg></appmsg></msg>')


def parse_message(xml, raw_type=PACKED_REFERENCE, mention="wxid_self"):
    store = WeChatStore.__new__(WeChatStore)
    row = {"local_id": 50, "local_type": raw_type, "create_time": NOW,
           "message_content": "wxid_friend:\n"+xml, "real_sender_id": 1,
           "source": f"<msgsource><atuserlist>{mention}</atuserlist></msgsource>"}
    return display_message(store._parse_message("synthetic@chatroom", row, {}, {1: "wxid_friend"}))


class ReferenceParsingTests(unittest.TestCase):
    def test_real_packed_type_extracts_question_and_old_quoted_content(self):
        parsed = parse_message(quoted_xml())
        self.assertEqual(parsed["type"], 1)
        self.assertEqual(parsed["raw_type"], 244813135921)
        self.assertEqual(parsed["message_kind"], "quoted_text")
        self.assertEqual(parsed["text"], "@ds 这怎么看")
        self.assertEqual(parsed["reference"]["text"], "明天 10 点开会")
        self.assertEqual(parsed["reference"]["sender_name"], "小明")
        self.assertEqual(parsed["reference"]["time"], NOW-10000)
        self.assertEqual(parsed["mentioned_users"], ["wxid_self"])

    def test_legacy_49_reference_and_long_title_not_truncated(self):
        question, reference = parse_quoted_reply(quoted_xml("@ds "+"长"*1000), 49)
        self.assertEqual(len(question), 1004)
        self.assertEqual(reference["kind"], "text")

    def test_non_reference_card_and_malformed_xml_never_become_plaintext(self):
        for raw_type, xml in [(49, "<msg><appmsg><type>5</type><title>链接</title></appmsg></msg>"),
                              (PACKED_REFERENCE, "<msg>broken"), ((5<<32)|49, quoted_xml()),
                              (PACKED_REFERENCE, '<!DOCTYPE msg [<!ENTITY x "X">]><msg/>')]:
            with self.subTest(raw_type=raw_type):
                parsed = parse_message(xml, raw_type)
                self.assertNotEqual(parsed["type"], 1)

    def test_embedded_old_mention_does_not_replace_new_message_mention(self):
        parsed = parse_message(quoted_xml(), mention="")
        self.assertEqual(parsed["mentioned_users"], [])

    def test_images_and_missing_reference_are_explicitly_unavailable(self):
        reference = parse_message(quoted_xml(content="<img cdn='private' />", kind=3))["reference"]
        self.assertEqual(reference["kind"], "image")
        self.assertFalse(reference["available"])
        self.assertEqual(reference["text"], "")
        parsed = parse_message("<msg><appmsg><type>57</type><title>@ds 怎么看</title></appmsg></msg>")
        self.assertFalse(parsed["reference"]["available"])

    def test_link_snapshot_has_title_description_url_but_never_claims_full_page(self):
        link = "<msg><appmsg><type>5</type><title>标题</title><des>摘要</des><url>https://example.invalid/a</url></appmsg></msg>"
        reference = parse_message(quoted_xml(content=link, kind=49))["reference"]
        self.assertEqual(reference["kind"], "link")
        self.assertIn("标题\n摘要", reference["text"])
        self.assertIn("未抓取网页正文", reference["reason"])

    def test_nested_references_are_structured_and_depth_bounded(self):
        xml = quoted_xml()
        for _ in range(8):
            xml = quoted_xml(content=xml, kind=49)
        reference = parse_message(xml)["reference"]
        depth = 0
        while "reference" in reference:
            reference = reference["reference"]
            depth += 1
        self.assertLessEqual(depth, 4)
        self.assertFalse(reference["available"])

    def test_database_query_pipeline_handles_reference_with_sender_and_metadata(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE Msg_test(local_id INTEGER, local_type INTEGER, create_time INTEGER, message_content TEXT, real_sender_id INTEGER, source TEXT)")
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("INSERT INTO Name2Id VALUES ('wxid_friend')")
        conn.execute("INSERT INTO Msg_test VALUES (50,?,?,?,?,?)", (PACKED_REFERENCE, NOW, quoted_xml(), 1,
                     "<msgsource><atuserlist>wxid_self</atuserlist></msgsource>"))
        store = WeChatStore.__new__(WeChatStore)
        store.message_table = Mock(return_value=("test.db", "Msg_test"))
        store.contacts = Mock(return_value={})
        store._connect = Mock(return_value=conn)
        rows = store.messages_after("synthetic@chatroom", 49)
        self.assertEqual(rows[0]["reference"]["text"], "明天 10 点开会")
        self.assertEqual(rows[0]["sender"], "wxid_friend")


class ReferencePromptTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

        self.cfg['system_prompt_file'] = str(Path(__file__).resolve().parents[1]/'prompts/system.md')

    def test_explicit_old_reference_included_but_unrelated_old_history_excluded(self):
        trigger = parse_message(quoted_xml(content="EXPLICIT_OLD_QUOTE"))
        history = [msg(1, NOW-10000, "UNRELATED_OLD_SECRET"), msg(2, NOW-1, "WINDOW_TEXT"), trigger]
        prompt = build_prompt(self.cfg, history, trigger, "合成群")[1]["content"]
        self.assertIn("EXPLICIT_OLD_QUOTE", prompt)
        self.assertIn("WINDOW_TEXT", prompt)
        self.assertNotIn("UNRELATED_OLD_SECRET", prompt)
        self.assertEqual(prompt.count("@ds 这怎么看"), 1)
        self.assertIn("小明", prompt)
        self.assertIn('"member": "wxid_original"', prompt)
        self.assertNotIn("1234567890123456789", prompt)
        self.assertNotIn("<refermsg>", prompt)

    def test_unavailable_media_and_nested_quote_have_clear_boundary(self):
        trigger = parse_message(quoted_xml(content="IMAGE_PRIVATE_XML", kind=3))
        prompt = build_prompt(self.cfg, [], trigger, "合成群")[1]["content"]
        self.assertIn('"available": false', prompt)
        self.assertIn("不要猜测图片", prompt)
        self.assertNotIn("IMAGE_PRIVATE_XML", prompt)

    def test_reference_counts_towards_budget_without_silent_truncation(self):
        self.cfg["target"]["context_max_chars"] = 300
        trigger = parse_message(quoted_xml(content="长"*1000))
        with self.assertRaises(ContextError):
            build_prompt(self.cfg, [], trigger, "合成群")

    def test_previous_context_quote_remains_explicitly_bound_to_its_actual_author(self):
        old = parse_message(quoted_xml(content="UNREQUESTED_REFERENCE"))
        old.update(local_id=49, time=NOW-1)
        trigger = msg(50, NOW, "@ds 普通问题")
        prompt = build_prompt(self.cfg, [old], trigger, "合成群")[1]["content"]
        self.assertIn("UNREQUESTED_REFERENCE", prompt)
        self.assertIn('"member": "wxid_original"', prompt)
        self.assertIn(old["text"], prompt)


if __name__ == "__main__":
    unittest.main()
