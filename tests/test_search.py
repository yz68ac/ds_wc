"""联网工具与反馈：全为合成响应，不调用 API。"""
from tests.support.model_support import default_config
from contextlib import nullcontext
import copy
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

from tests.support.model_support import ClientFixture, ModelError
from wxbridge.ai.search import SearchClient, SearchError, answer_with_search, is_public_url_candidate
from wxbridge.config import DEFAULTS, _deep_merge


def call(name="web_search", **arguments):
    return dict(id="synthetic-call", type="function", function=dict(name=name, arguments=json.dumps(arguments)))


def result(url="https://example.com/release", title="官方说明", summary="合成网页资料"):
    return dict(results=[dict(title=title, url=url, summary=summary, date="2026-10-20")])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.messages = [dict(role="system", content="合成人格"), dict(role="user", content="合成问题")]
        self.client = Mock()
        from provider.adapters import get_protocol_adapter
        self.client.assistant_message.side_effect = get_protocol_adapter('deepseek').assistant_message
        self.backend = Mock()
        self.backend.search.return_value = result()
        self.backend.fetch.return_value = dict(title="详情", content="实际合成正文")
        self.notice = Mock()

    def answer(self):
        return answer_with_search(self.cfg, self.client, self.messages, self.notice, search_client=self.backend)

    def test_smalltalk_no_search_no_feedback(self):
        self.client.complete.return_value = dict(content="聊得不错")
        text, meta = self.answer()
        self.assertEqual(text, "聊得不错")
        self.assertFalse(meta["attempted"])
        self.notice.assert_not_called()
        self.backend.search.assert_not_called()

    def test_empty_final_content_is_a_retryable_failure(self):
        for content in (None, '', ' \n ', '---', '```json\n```'):
            with self.subTest(content=content):
                self.client.complete.return_value = dict(content=content, reasoning_content='PRIVATE_REASONING')
                with self.assertRaisesRegex(ModelError, 'empty_reply'):
                    self.answer()
        self.backend.search.assert_not_called()

    def test_empty_reply_after_search_is_a_failure_not_silence(self):
        for rows in (result(), dict(results=[])):
            with self.subTest(found=bool(rows['results'])):
                self.client.complete.side_effect = [dict(tool_calls=[call(query='合成查询')]), dict(content='')]
                self.backend.search.return_value = rows
                with self.assertRaisesRegex(ModelError, 'empty_reply'):
                    self.answer()

    def test_feedback_off_discards_tool_waiting_draft_and_preserves_search_metadata(self):
        self.assertFalse(self.cfg['search']['feedback_enabled'])
        self.client.complete.side_effect = [dict(content='我去看看，等我一下', tool_calls=[call(query='合成更新')]),
                                           dict(content='这次主要改了速度')]
        text, meta = self.answer()
        self.assertNotIn('我去看看', text)
        self.assertIn('这次主要改了速度', text)
        self.assertNotIn('https://example.com/release', text)
        self.assertTrue(meta['attempted'])
        self.assertEqual(meta['calls'], 1)
        # 此回调是内部事件，主进程开关控制是否加入群发送队列。
        self.notice.assert_called_once()

    def test_tool_result_then_final_with_real_sources_and_feedback_first(self):
        self.client.complete.side_effect = [dict(content="不要直接发送的草稿", tool_calls=[call(query="最新合成信息")], reasoning_content="合成思考"), dict(content="核实后的回答")]
        self.backend.search.side_effect = lambda *args: (self.notice.assert_called_once(), result())[1]
        text, meta = self.answer()
        self.assertIn("核实后的回答", text)
        self.assertNotIn("https://example.com/release", text)
        self.assertNotIn("草稿", text)
        self.assertTrue(meta["attempted"])
        self.assertEqual(meta["calls"], 1)
        self.assertNotIn("最新合成信息", json.dumps(meta))
        conversation = self.client.complete.call_args.args[0]
        self.assertEqual(conversation[-2]["reasoning_content"], "合成思考")
        self.assertEqual(conversation[-1]["role"], "tool")
        self.assertIn("合成网页资料", conversation[-1]["content"])
        self.assertEqual(self.messages[0]["content"], "合成人格")

    def test_multimodal_input_kept_and_not_copied_into_search_query(self):
        self.messages[1]["content"] = [dict(type="text", text="这张图对应的产品有新消息吗"), dict(type="image_url", image_url=dict(url="data:image/png;base64,AAAA"))]
        saved = copy.deepcopy(self.messages)
        self.client.complete.side_effect = [dict(tool_calls=[call(query="合成产品 最新消息")]), dict(content="新消息")]
        self.answer()
        self.assertEqual(self.client.complete.call_args_list[0].args[0][1], saved[1])
        self.assertEqual(self.backend.search.call_args.args[0], "合成产品 最新消息")
        self.assertEqual(self.messages, saved)

    def test_fetch_only_previously_returned_is_public_url_candidate(self):
        self.client.complete.side_effect = [dict(tool_calls=[call(query="合成资料")]),
            dict(tool_calls=[call("web_fetch", url="https://example.com/release")]), dict(content="读到详情")]
        self.answer()
        self.backend.fetch.assert_called_once()
        self.notice.assert_called_once()

    def test_short_plain_reply_keeps_source_internal_without_attaching_a_url(self):
        self.backend.search.return_value = dict(results=[*result()['results'], *result('https://example.com/detail')['results']])
        self.client.complete.side_effect = [dict(tool_calls=[call(query='合成资料')]),
            dict(tool_calls=[call('web_fetch', url='https://example.com/detail')]), dict(content='**查到了**，这次主要改了速度。')]
        text, _ = self.answer()
        self.assertEqual(text, '查到了，这次主要改了速度。')
        rules = self.client.complete.call_args.args[0][0]['content']
        self.assertTrue(rules.startswith('合成人格'))
        self.assertIn('只发纯文本', rules)

    def test_existing_primary_url_not_repeated(self):
        self.client.complete.side_effect = [dict(tool_calls=[call(query='资料')]),
            dict(content='看这个：[说明](https://example.com/release)')]
        text, _ = self.answer()
        self.assertEqual(text.count('https://example.com/release'), 1)
        self.assertNotIn('](', text)

    def test_unknown_private_url_and_malformed_query_never_execute(self):
        for tool in (call("unknown", query="x"), call(query="x"*129), call("web_fetch", url="http://127.0.0.1/private"),
                     call("web_fetch", url="https://unknown.example.com/page")):
            with self.subTest(tool=tool["function"]["name"]):
                self.client.complete.side_effect = [dict(tool_calls=[tool]), dict(content="未联网")]
                _, meta = self.answer()
                self.assertFalse(meta["attempted"])
        self.backend.search.assert_not_called()
        self.backend.fetch.assert_not_called()
        self.notice.assert_not_called()

    def test_malformed_arguments_no_leak(self):
        tool = call()
        tool["function"]["arguments"] = "PRIVATE_BAD_JSON"
        self.client.complete.side_effect = [dict(tool_calls=[tool]), dict(content="换个说法")]
        _, meta = self.answer()
        self.assertEqual(meta["errors"], ["invalid_tool_arguments"])
        self.assertNotIn("PRIVATE", json.dumps(meta))

    def test_search_failure_and_empty_results_do_not_claim_verification(self):
        for failure in (SearchError("http_402"), {"results": []}):
            self.client.complete.side_effect = [dict(tool_calls=[call(query="最新信息")]), dict(content="模型不应假装已经核实")]
            self.backend.search.side_effect = failure if isinstance(failure, Exception) else None
            self.backend.search.return_value = failure
            text, meta = self.answer()
            self.assertIn("核实不了", text)
            self.assertNotIn("模型不应", text)
            self.assertEqual(meta["results"], 0)

    def test_call_budget_is_bounded_and_feedback_only_once(self):
        self.cfg["search"]["max_requests"] = 1
        self.client.complete.side_effect = [dict(tool_calls=[call(query="first"), call(query="second")]), dict(content="完成")]
        _, meta = self.answer()
        self.backend.search.assert_called_once()
        self.notice.assert_called_once()
        self.assertEqual(meta["calls"], 1)
        self.assertEqual(self.client.complete.call_args.kwargs["tool_choice"], "none")

    def test_final_model_failure_after_search_returns_clear_fallback(self):
        self.client.complete.side_effect = [dict(tool_calls=[call(query="查询")]), ModelError("synthetic")]
        text, meta = self.answer()
        self.assertIn("超时", text)
        self.assertNotIn("https://example.com/release", text)
        self.assertIn("answer_generation_failed", meta["errors"])

    def test_initial_model_error_keeps_existing_failure_semantics(self):
        self.client.complete.side_effect = ModelError("synthetic")
        with self.assertRaises(ModelError):
            self.answer()
        self.notice.assert_not_called()


    def test_is_public_url_candidates(self):
        for url in ("http://127.0.0.1", "http://10.0.0.1", "file:///etc/passwd", "https://user:pass@example.com", "http://host.local", "http://[::1]/"):
            self.assertFalse(is_public_url_candidate(url))
        self.assertTrue(is_public_url_candidate("https://example.com/release"))

    def test_complete_preserves_tool_calls_while_chat_only_returns_content(self):
        client = ClientFixture.__new__(ClientFixture)
        from provider.adapters import get_protocol_adapter
        client.adapter = get_protocol_adapter('deepseek')
        from wxbridge.ai.roles import model_settings
        client.cfg = model_settings(self.cfg)
        client._post = Mock(return_value={"choices": [{"message": {"content": "最终回答", "tool_calls": [call(query="x")]}}]})
        self.assertTrue(client.complete([], tools=[{}])["tool_calls"])
        self.assertEqual(client.chat([]), "最终回答")

    def test_high_effort_is_sent_when_enabled_and_hidden_reasoning_is_not_reply(self):
        client = ClientFixture.__new__(ClientFixture)
        from provider.adapters import get_protocol_adapter
        client.adapter = get_protocol_adapter('deepseek')
        from wxbridge.ai.roles import model_settings
        client.cfg = dict(model_settings(self.cfg), thinking='enabled', reasoning_effort='high')
        client._post = Mock(return_value={'choices':[{'message':{'content':'简短回答','reasoning_content':'PRIVATE_REASONING'}}]})
        self.assertEqual(client.chat([]),'简短回答')
        payload = client._post.call_args.args[0]
        self.assertEqual(payload['thinking'],{'type':'enabled'})
        self.assertEqual(payload['reasoning_effort'],'high')
        client.cfg['thinking']='disabled'
        client.complete([])
        self.assertNotIn('reasoning_effort',client._post.call_args.args[0])
