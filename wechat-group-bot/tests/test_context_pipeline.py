"""真实 SQLite + 可控时钟/子进程替身，覆盖接收至发送确认，不联网。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from wxbridge.app.bot import WeChatDeepSeekBot
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.ai.client import DeepSeekClient
from wxbridge.storage.journal import Journal
from wxbridge.app.workers import ProcessJob
from synthetic import NOW, msg
from test_references import parse_message, quoted_xml


class FakeJob:
    def __init__(self, kind, payload, **kwargs):
        self.kind, self.payload, self.result, self.closed = kind, payload, None, False

    def poll(self):
        return self.result

    def close(self):
        self.closed = True


class PipelineTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.cfg = _deep_merge(DEFAULTS, {})
        self.cfg["target"].update(chat="合成群", cooldown_seconds=0)
        self.cfg["ui"]["dry_run"] = False
        self.cfg["queue"]["file"] = str(Path(self.temp.name) / "queue.sqlite3")
        self.cfg["queue"]["debounce_seconds"] = 3  # 保留后续可选合并策略的回归。
        self.cfg["state_file"] = str(Path(self.temp.name) / "state.json")
        self.cfg["deepseek"].update(system_prompt_file="", system_prompt="合成人格")
        self.now, self.rows, self.jobs = float(NOW), [], []
        self.store = Mock()
        self.store.self_wxid = "wxid_self"
        self.store.resolve_chat.return_value = ("synthetic@chatroom", "合成群")
        self.store.last_local_id.side_effect = lambda chat: max([m["local_id"] for m in self.rows], default=0)
        self.store.messages_after.side_effect = lambda chat, cursor, limit: [m for m in self.rows if m["local_id"] > cursor][:limit]
        self.store.messages_in_window.side_effect = lambda chat, start, end: list(self.rows)
        for target, value in [("wxbridge.app.bot.WeChatStore", self.store),
                              ("wxbridge.storage.journal.checked_path", lambda value, **kw: Path(value))]:
            p = patch(target, return_value=value) if target.endswith("WeChatStore") else patch(target, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("wxbridge.app.bot.time.time", side_effect=lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.make_bot()
        self.addCleanup(lambda: self.bot.close())

    def make_bot(self):
        self.bot = WeChatDeepSeekBot(self.cfg)
        def make(kind, payload, **kwargs):
            job = FakeJob(kind, payload, **kwargs)
            self.jobs.append(job)
            return job
        self.bot.job_factory = make

    def restart(self):
        self.bot.close()
        self.make_bot()

    def add(self, identifier, sender="wxid_friend", text=None, sent=None, mention=True):
        item = msg(identifier, int(self.now) if sent is None else sent,
                   text or f"@DeepSeek 问题{identifier}", sender)
        item["mentioned_users"] = ["wxid_self"] if mention else []
        self.rows.append(item)
        return item

    def generated(self, answer="合成回复"):
        for job in self.jobs:
            if job.kind == "generate" and not job.closed:
                job.result = {"ok": True, "reply": answer}
        self.bot.tick()

    def sends(self):
        return [job for job in self.jobs if job.kind == "send"]

    def test_immediate_generation_merge_reset_and_single_final_send(self):
        first = self.add(1)
        self.bot.tick()
        old = self.jobs[0]
        self.assertEqual(old.kind, "generate")
        self.assertFalse(self.sends())
        self.now += 2
        second = self.add(2)
        old.result = {"ok": True, "reply": "STALE_REPLY"}
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertEqual(self.bot.journal.head()["version"], 2)
        prompt = self.jobs[-1].payload[1][1]["content"]
        self.assertEqual(prompt.count(first["text"]), 1)
        self.assertEqual(prompt.count(second["text"]), 1)
        self.generated()
        self.now += 2.9
        self.bot.tick()
        self.assertFalse(self.sends())
        self.now += .1
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0].payload[2], "合成回复")
        self.add(3, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.status()["queue_length"], 0)
        self.assertEqual(len(self.sends()), 1)

    def test_quoted_image_binding_only_in_main_and_queue_never_contains_pixels(self):
        self.cfg["queue"]["debounce_seconds"] = 0
        item = self.add(1)
        item["reference"] = dict(kind="image", available=False, text="", image_md5="a"*32)
        self.store.image_binding.return_value = dict(error="local_image_not_cached", candidates=[])
        self.bot.tick()
        self.store.image_binding.assert_called_once_with("synthetic@chatroom", item["reference"])
        self.assertEqual(len(self.jobs[0].payload), 4)
        self.assertEqual(self.jobs[0].payload[3]["images"][0]["trigger_index"], 0)
        self.jobs[0].result = dict(ok=True, reply="合成看图回答", vision=dict(images=1, references=1, items=[dict(available=True, quality="thumbnail")]))
        self.bot.tick()
        status = self.bot.journal.status()
        self.assertEqual(status["last_vision"]["images"], 1)
        self.assertNotIn("base64", json.dumps(self.bot.journal.tasks()))
        self.restart()
        self.assertEqual(self.bot.journal.status()["last_vision"]["images"], 1)

    def test_vision_disabled_does_not_resolve_images(self):
        self.cfg["vision"]["enabled"] = False
        item = self.add(1)
        item["reference"] = dict(kind="image", available=False, text="")
        self.bot.tick()
        self.store.image_binding.assert_not_called()
        self.assertEqual(len(self.jobs[0].payload), 3)

    def test_zero_debounce_each_real_mention_is_independent_even_same_poll(self):
        self.cfg["queue"]["debounce_seconds"] = 0
        self.add(1)
        self.add(2)
        self.bot.tick()
        tasks = self.bot.journal.tasks()
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["phase"] == "queued" and t["version"] == 1 for t in tasks))
        self.assertEqual(len(self.bot.generators), 2)
        self.generated()
        self.assertEqual(len(self.sends()), 1)  # 无需等待 3 秒。

    def test_ordinary_messages_do_not_reset_and_sender_isolation_closure_order(self):
        self.add(1, sender="A")
        self.bot.tick()
        self.now += 1
        self.add(2, sender="B")
        self.bot.tick()
        self.now += 1
        self.add(3, sender="A")
        self.bot.tick()
        self.now += 1
        self.add(4, sender="B", mention=False)
        self.generated()
        self.now += 1
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()["sender"], "B")
        self.assertEqual(self.bot.journal.head()["version"], 1)
        self.assertEqual(len(self.sends()), 1)

    def test_frozen_batch_gets_new_batch_and_old_result_rejected(self):
        self.add(1)
        self.bot.tick()
        self.now += 3
        self.bot.tick()
        self.add(2)
        self.bot.tick()
        self.assertEqual(len(self.bot.journal.tasks()), 2)
        self.assertFalse(self.bot.journal.generated(1, 0, ["LATE_SECRET"]))

    def test_slow_api_keeps_receiving_and_concurrency_bounded(self):
        for identifier, sender in [(1, "A"), (2, "B"), (3, "C")]:
            self.add(identifier, sender=sender)
            self.bot.tick()
        self.assertEqual(len(self.bot.generators), 2)
        self.assertEqual(self.bot.last_local_id, 3)
        self.now += 2
        self.add(4, sender="A")
        old = self.jobs[0]
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertEqual(len(self.bot.generators), 2)
        self.assertEqual(self.bot.last_local_id, 4)

    def test_cooldown_wait_and_admitted_task_survives_90_seconds(self):
        self.cfg["target"]["cooldown_seconds"] = 200
        with self.bot.journal.db:
            self.bot.journal.set("last_success", self.now)
        self.add(1)
        self.bot.tick()
        self.generated()
        self.now += 100
        self.bot.tick()
        self.assertFalse(self.sends())
        self.assertEqual(len(self.bot.journal.tasks()), 1)
        self.now += 100
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)

    def test_age_only_at_intake_and_real_mention_required(self):
        for identifier, options in [(1, dict(sender="wxid_self")), (2, dict(sent=NOW-91)),
                                    (3, dict(sent=NOW+1)), (4, dict(sender="")),
                                    (5, dict(mention=False))]:
            self.add(identifier, **options)
        self.bot.tick()
        self.assertFalse(self.jobs)
        self.assertEqual(self.bot.last_local_id, 5)
        valid = self.add(6)
        self.assertTrue(self.bot._triggered(valid))
        for mentions in ([], None, ["wxid_self_other"], ["notify@all"]):
            self.assertFalse(self.bot._triggered({**valid, "mentioned_users": mentions}))

    def test_cursor_idempotence_and_pagination(self):
        for identifier in range(1, 205):
            self.add(identifier, mention=identifier==204)
        self.bot.tick()
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 204)
        self.assertEqual(len(self.bot.journal.tasks()), 1)
        self.assertEqual(self.bot.journal.head()["version"], 1)

    def test_cursor_and_intake_transaction_roll_back_together(self):
        items = [self.add(1), self.add(2)]
        def accept(item):
            if item["local_id"] == 2:
                raise RuntimeError("synthetic rollback")
            return True
        with self.assertRaises(RuntimeError):
            self.bot.journal.intake(items, accept, self.now, 3)
        self.assertEqual(self.bot.last_local_id, 0)
        self.assertFalse(self.bot.journal.tasks())

    def test_merged_context_uses_latest_trigger_time_and_excludes_all_questions(self):
        self.add(1, text="OLD_SECRET", sent=NOW-601, mention=False)
        self.add(2, text="BOUNDARY", sent=NOW-598, mention=False)
        first = self.add(3)
        self.bot.tick()
        self.now += 2
        second = self.add(4)
        self.bot.tick()
        self.store.messages_in_window.assert_called_with("synthetic@chatroom", NOW-598, NOW+2)
        prompt = self.jobs[-1].payload[1][1]["content"]
        self.assertIn("BOUNDARY", prompt)
        self.assertNotIn("OLD_SECRET", prompt)
        for question in (first, second):
            self.assertEqual(prompt.count(question["text"]), 1)

    def ready(self, answer="合成回复"):
        self.add(1)
        self.bot.tick()
        self.generated(answer)
        self.now += 3

    def test_restart_generation_resumes_without_moving_cursor(self):
        self.add(1)
        self.bot.tick()
        old = self.jobs[-1]
        self.restart()
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertEqual(self.bot.last_local_id, 1)
        self.assertEqual(self.bot.journal.head()["version"], 1)
        self.assertEqual(len(self.bot.generators), 1)

    def test_restart_before_submit_uses_persisted_ready_reply(self):
        self.ready()
        count = len(self.jobs)
        self.restart()
        self.bot.tick()
        self.assertEqual(len(self.jobs), count+1)
        self.assertEqual(self.jobs[-1].kind, "send")

    def test_restart_after_send_confirms_without_resending_and_continues_chunks(self):
        self.cfg["target"]["max_reply_chars"] = 4
        self.ready("abcdefgh")
        self.bot.tick()
        first = self.sends()[0].payload[2]
        self.add(2, sender="wxid_self", text=first, mention=False)
        self.restart()
        self.bot.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertNotEqual(self.sends()[1].payload[2], first)
        self.assertEqual(self.bot.journal.head()["next_chunk"], 1)

    def test_intent_before_click_crash_is_not_blindly_retried(self):
        self.ready()
        self.bot.journal.seal(self.now)
        self.bot.journal.intend(1, 1, self.now, 10)
        self.restart()
        self.bot.tick()
        self.assertFalse(self.sends())
        self.now += 11
        self.bot.tick()
        self.assertIn("send_confirmation_timeout", self.bot.journal.head()["blocked_reason"])

    def test_ui_failure_timeout_keeps_intake_and_late_confirmation_unblocks(self):
        self.ready()
        self.bot.tick()
        self.sends()[0].result = {"ok": False, "error": "UIError"}
        self.bot.tick()
        self.now += 11
        self.bot.tick()
        self.assertTrue(self.bot.journal.head()["blocked_reason"])
        self.add(2)
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 2)
        self.assertEqual(len(self.bot.journal.tasks()), 2)
        self.assertEqual(len(self.sends()), 1)
        self.add(3, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(1)["phase"], "done")

    def test_only_new_own_exact_outbound_record_confirms(self):
        self.add(1, sender="wxid_self", text="合成回复", mention=False)
        self.add(2)
        self.bot.tick()
        self.generated()
        self.now += 3
        self.bot.tick()
        self.add(3, sender="another", text="合成回复", mention=False)
        self.add(4, sender="wxid_self", text="合成回复 ", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()["next_chunk"], 0)
        self.store.messages_after.assert_any_call("synthetic@chatroom", 2, limit=100)
        self.add(5, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertFalse(self.bot.journal.tasks())

    def test_identity_mismatch_rejects_recovery(self):
        with self.assertRaisesRegex(ValueError, "账号或目标群"):
            Journal(self.cfg["queue"]["file"], {"account": "other", "chat": "wrong"}, 0)

    def test_quoted_real_mention_reaches_generation_send_and_confirmation(self):
        question = parse_message(quoted_xml(question="@ds QUESTION_SECRET", content="REFERENCE_SECRET"))
        self.rows.append(question)
        with self.assertLogs("wxbridge", level="INFO") as captured:
            self.bot.tick()
        prompt = self.jobs[-1].payload[1][1]["content"]
        self.assertIn("QUESTION_SECRET", prompt)
        self.assertIn("REFERENCE_SECRET", prompt)
        self.assertNotIn("QUESTION_SECRET", "\n".join(captured.output))
        self.assertNotIn("REFERENCE_SECRET", "\n".join(captured.output))
        self.generated()
        self.now += 3
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.add(51, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.status()["queue_length"], 0)
        self.assertEqual(self.bot.journal.task(1)["trigger_ids"], [50])
        status = json.dumps(self.bot.journal.status(), ensure_ascii=False)
        self.assertNotIn("QUESTION_SECRET", status)
        self.assertNotIn("REFERENCE_SECRET", status)

    def test_quote_without_new_real_mention_is_rejected_with_reason(self):
        self.rows.append(parse_message(quoted_xml(), mention=""))
        self.bot.tick()
        self.assertFalse(self.jobs)
        event = self.bot.journal.status()["recent_intake"][-1]
        self.assertEqual(event["reason"], "not_mentioned")
        self.assertEqual(event["kind"], "quoted_text")
        self.assertEqual(event["reference_kind"], "text")
        self.assertFalse(event["real_mention"])

    def test_intake_diagnostics_commit_with_cursor_and_queue(self):
        rows = [self.add(1), self.add(2)]
        def describe(item):
            if item["local_id"] == 2:
                raise RuntimeError("synthetic diagnostic failure")
            return {"local_id": 1, "reason": "accepted"}
        with self.assertRaises(RuntimeError):
            self.bot.journal.intake(rows, lambda m: True, self.now, 3, describe=describe)
        self.assertEqual(self.bot.last_local_id, 0)
        self.assertFalse(self.bot.journal.tasks())
        self.assertEqual(self.bot.journal.get("recent_intake", []), [])

    def test_ui_metadata_is_saved_when_database_confirms_before_ui_returns(self):
        self.ready()
        self.bot.tick()
        job = self.sends()[0]
        self.add(2, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(1)["phase"], "done")
        job.result = {"ok": True, "ui": {"action": "x11-click", "after_chars": 0, "hit_native": True}}
        self.bot.tick()
        self.assertEqual(self.bot.journal.task(1)["last_ui"]["action"], "x11-click")

    def test_proven_pre_submit_failure_retries_bounded_and_unknown_click_never_retries(self):
        self.ready()
        self.bot.tick()
        for attempt in range(3):
            job = self.sends()[-1]
            job.result = {"ok": False, "ui": {"error": "command_timeout", "operation": {"submitted": False}}}
            self.bot.tick()
            count = len(self.sends())
            self.bot.tick()
            self.assertEqual(len(self.sends()), count)
            self.now += 2
            self.bot.tick()
        self.assertEqual(len(self.sends()), 3)
        self.assertIsNotNone(self.bot.journal.head()["intent"])
        self.now += 11
        self.bot.tick()
        self.assertTrue(self.bot.journal.head()["blocked_reason"])

    def test_unknown_click_does_not_enter_pre_submit_retry(self):
        self.ready()
        self.bot.tick()
        self.sends()[-1].result = {"ok": False, "ui": {"error": "command_timeout", "operation": {"submitted": None}}}
        self.bot.tick()
        self.now += 3
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)

    def test_status_and_logs_do_not_include_bodies_or_key(self):
        self.add(1, text="CHAT_SECRET")
        with self.assertLogs("wxbridge", level="INFO") as captured:
            self.bot.tick()
            self.generated("REPLY_SECRET")
            self.now += 3
            self.bot.tick()
        output = "\n".join(captured.output) + self.bot.status_path.read_text(encoding="utf-8")
        for secret in ("CHAT_SECRET", "REPLY_SECRET", "sk-test-secret"):
            self.assertNotIn(secret, output)
        self.assertTrue(self.bot.journal.head()["intent"])

    def test_reasoning_is_not_sent_as_answer(self):
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.cfg = {**self.cfg["deepseek"], "thinking": "disabled"}
        client._post = Mock(return_value={"choices": [{"message": {"content": "", "reasoning_content": "PRIVATE"}}]})
        self.assertEqual(client.chat([]), "")
        self.assertEqual(client._post.call_args.args[0]["thinking"], {"type": "disabled"})


    def search_started(self):
        generator = next(job for job in self.jobs if job.kind == "generate" and not job.closed)
        generator.result = {"event": "search_started"}
        self.bot.tick()
        generator.result = None
        return generator

    def test_search_notice_then_final_reply_both_confirmed_and_cooldown(self):
        self.cfg["queue"]["debounce_seconds"] = 0
        self.cfg["target"]["cooldown_seconds"] = 8
        self.add(1)
        self.bot.tick()
        generator = self.search_started()
        notice = self.sends()[0].payload[2]
        self.assertIn(notice, self.cfg["search"]["feedback_phrases"])
        self.assertEqual(self.bot.journal.head()["intent"]["kind"], "notice")
        self.sends()[0].result = dict(ok=True, ui=dict(submitted=True))
        self.add(2, sender="wxid_self", text=notice, mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()["notice"]["state"], "confirmed")
        self.assertEqual(self.bot.journal.head()["next_chunk"], 0)
        generator.result = dict(ok=True, reply="联网最终回答", search=dict(attempted=True, calls=1, results=2, errors=[]))
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.now += 8
        self.bot.tick()
        self.assertEqual(self.sends()[1].payload[2], "联网最终回答")
        self.sends()[1].result = dict(ok=True, ui=dict(submitted=True))
        self.add(3, sender="wxid_self", text="联网最终回答", mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.status()["queue_length"], 0)
        task = self.bot.journal.task(1)
        self.assertEqual(task["last_notice_ack_id"], 2)
        self.assertEqual(task["last_ack_id"], 3)
        self.assertEqual(task["notice"]["text"], "")

    def test_search_notice_is_not_repeated_for_repeated_events_or_after_restart(self):
        self.cfg["queue"]["debounce_seconds"] = 0
        self.add(1)
        self.bot.tick()
        generator = self.search_started()
        notice = self.sends()[0].payload[2]
        generator.result = dict(event="search_started")
        self.bot.tick()
        self.assertEqual(len(self.sends()), 1)
        self.restart()  # 提交后、数据库确认前重启，先查发出记录。
        self.add(2, sender="wxid_self", text=notice, mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.head()["notice"]["state"], "confirmed")
        self.search_started()
        self.assertEqual(len(self.sends()), 1)  # 只保留首次旧发送对象，没有新增提示发送。

    def test_search_notice_confirmation_timeout_blocks_sends_but_keeps_receiving(self):
        self.cfg["queue"]["debounce_seconds"] = 0
        self.add(1)
        self.bot.tick()
        self.search_started()
        self.sends()[0].result = dict(ok=True, ui=dict(submitted=True))
        self.bot.tick()
        self.now += 11
        self.add(2)
        self.bot.tick()
        self.assertEqual(self.bot.last_local_id, 2)
        self.assertIn("send_confirmation_timeout", self.bot.journal.head()["blocked_reason"])
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.bot.journal.tasks()), 2)

    def test_old_merged_version_unsent_notice_is_discarded(self):
        self.add(1)
        self.bot.tick()
        old = self.search_started()
        self.assertFalse(self.sends())
        self.now += 1
        self.add(2)
        self.bot.tick()
        self.assertTrue(old.closed)
        self.assertIsNone(self.bot.journal.head().get("notice"))
        self.generated("新版本直接回答")
        self.now += 3
        self.bot.tick()
        self.assertEqual([job.payload[2] for job in self.sends()], ["新版本直接回答"])

    def test_search_feedback_can_be_disabled(self):
        self.cfg["search"]["feedback_enabled"] = False
        self.cfg["queue"]["debounce_seconds"] = 0
        self.add(1)
        self.bot.tick()
        self.search_started()
        self.assertFalse(self.sends())
        self.generated("直接最终回复")
        self.assertEqual(len(self.sends()), 1)


class WorkerTests(unittest.TestCase):
    def test_search_disabled_still_uses_plain_text_and_output_preferences(self):
        from wxbridge.app.workers import _work
        import logging
        prior = logging.root.manager.disable
        connection = Mock()
        try:
            with patch("wxbridge.ai.client.DeepSeekClient") as client:
                client.return_value.chat.return_value = "**简单说**，先这样。"
                _work(connection, "generate", ({"search": {"enabled": False}}, [dict(role="system", content="合成人格")], None))
                prompt = client.return_value.chat.call_args.args[0]
                self.assertTrue(prompt[0]["content"].startswith("合成人格"))
                self.assertIn("只输出纯文本", prompt[0]["content"])
        finally:
            logging.disable(prior)
        self.assertEqual(connection.send.call_args.args[0], dict(ok=True, reply="简单说，先这样。"))

    def test_progress_event_does_not_replace_final_result(self):
        from wxbridge.app.workers import _work
        import logging
        prior = logging.root.manager.disable
        connection = Mock()
        def answer(cfg, client, prompt, on_search):
            on_search()
            return "联网回答", dict(attempted=True, calls=1, results=1, errors=[])
        try:
            with patch("wxbridge.ai.client.DeepSeekClient"), patch("wxbridge.ai.search.answer_with_search", side_effect=answer):
                _work(connection, "generate", ({}, [], None))
        finally:
            logging.disable(prior)
        self.assertEqual(connection.send.call_args_list[0].args[0], dict(event="search_started"))
        self.assertEqual(connection.send.call_args_list[1].args[0]["reply"], "联网回答")
        self.assertTrue(connection.send.call_args_list[1].args[0]["ok"])

    def test_real_spawn_echo_process_no_network(self):
        job = ProcessJob("generate", ({}, [], "本地进程测试"), timeout=10)
        try:
            deadline = time.monotonic()+10
            result = None
            while result is None and time.monotonic() < deadline:
                result = job.poll()
                time.sleep(.02)
            self.assertEqual(result, {"ok": True, "reply": "本地进程测试"})
        finally:
            job.close()


if __name__ == "__main__":
    unittest.main()
