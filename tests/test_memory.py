"""合成身份、可控时间和假工作进程；绝不读真实聊天或调用 API。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import os

from wxbridge.ai.memory import snapshot, member_labels, extract
from wxbridge.ai.prompts import build_prompt
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.storage.person_memory import MemoryStore, DAY, fuzzy_time, affinity_value
from test_context_pipeline import PipelineTests
from synthetic import NOW, msg


IDENTITY = dict(account="wxid_bot", chat="test@chatroom")


def suggestion(label="U1", source="m1", delta=0, kind="preference", mode="real", replaces=None):
    return dict(updates=[dict(member=label, memories=[dict(kind=kind, mode=mode, summary="喜欢简洁直接的答复",
        sources=[source], confidence=.8, replaces=replaces)],
        affinity=dict(delta=delta, reason="本人礼貌交流" if delta else "", sources=[source] if delta else []))])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.memory = MemoryStore(self.temp.name, IDENTITY)
        self.cfg = _deep_merge(DEFAULTS, {})
        self.cfg["deepseek"].update(system_prompt_file="", system_prompt="测试人格")

    def snap(self, identifier=1, sender="wxid_a", name="同名", when=NOW, context=None):
        m = msg(identifier, when, "我喜欢简洁直接的回复", sender)
        m["sender_name"] = name
        return snapshot(self.cfg, context or [m], [m], IDENTITY, self.memory, when)

    def test_rename_same_name_and_account_group_isolation(self):
        self.memory.apply(suggestion(), self.snap(), NOW)
        self.memory.apply(suggestion(source="m2"), self.snap(2, name="新名字"), NOW)
        self.memory.apply(suggestion(source="m3"), self.snap(3, sender="wxid_b", name="新名字"), NOW)
        self.assertEqual(len(self.memory.data["members"]), 2)
        self.assertEqual(self.memory.data["members"]["wxid_a"]["aliases"], ["同名"])
        for identity in [dict(IDENTITY, account="other"), dict(IDENTITY, chat="other@chatroom")]:
            self.assertEqual(MemoryStore(self.temp.name, identity).status(NOW)["members"], 0)
        self.assertEqual(MemoryStore(self.temp.name, IDENTITY).status(NOW)["members"], 2)

    def test_aliases_keep_last_five(self):
        for i in range(1, 9):
            self.memory.apply(dict(updates=[]), self.snap(i, name=str(i)), NOW)
        self.assertEqual(self.memory.data["members"]["wxid_a"]["aliases"], ["3", "4", "5", "6", "7"])

    def test_unknown_foreign_third_party_and_stale_rejected_atomically(self):
        value = self.snap()
        for proposal in [suggestion(label="U9"), suggestion(source="m999"), suggestion(delta=4)]:
            with self.assertRaises(ValueError):
                self.memory.apply(proposal, value, NOW)
            self.assertEqual(self.memory.data["revision"], 0)
        wrong = copy.deepcopy(value)
        wrong["identity"]["chat"] = "other@chatroom"
        with self.assertRaises(ValueError):
            self.memory.apply(suggestion(), wrong, NOW)
        self.memory.apply(suggestion(), value, NOW)
        with self.assertRaises(ValueError):
            self.memory.apply(suggestion(), value, NOW)

    def test_score_daily_absolute_cap_sources_dedupe_and_decay(self):
        for i, delta in enumerate([3, -3, 3, 3, 3], 1):
            self.memory.apply(suggestion(source=f"m{i}", delta=delta), self.snap(i), NOW)
        p = self.memory.data["members"]["wxid_a"]
        self.assertEqual(p["affinity"], 54)
        self.assertEqual(sum(p["daily"].values()), 10)
        self.assertEqual(affinity_value(p, NOW+20*DAY), 54)
        self.assertEqual(affinity_value(p, NOW+21*DAY), 53)
        self.assertEqual(affinity_value(p, NOW+100*DAY), 50)
        old = self.snap(5)
        self.assertEqual(old["messages"], [])
        with self.assertRaises(ValueError):
            self.memory.apply(suggestion(source="m5", delta=1), old, NOW)

    def test_ttl_renewal_occurrence_distinct_and_capacity(self):
        self.memory.apply(suggestion(kind="event"), self.snap(), NOW)
        old = self.memory.data["members"]["wxid_a"]["entries"][0]
        self.memory.apply(suggestion(source="m2", kind="event", replaces=old["id"]), self.snap(2, when=NOW+DAY), NOW+DAY)
        entry = self.memory.data["members"]["wxid_a"]["entries"][0]
        self.assertEqual(entry["occurred_at"], NOW)
        self.assertEqual(entry["confirmed_at"], NOW+DAY)
        self.assertEqual(self.memory.status(NOW+31*DAY)["entries"], 0)
        for i in range(3, 18):
            update = suggestion(source=f"m{i}")
            update["updates"][0]["memories"][0]["summary"] = f"偏好{i}"
            self.memory.apply(update, self.snap(i, when=NOW+DAY), NOW+DAY)
        self.assertEqual(sum(e["kind"] == "preference" for e in self.memory.data["members"]["wxid_a"]["entries"]), 5)

    def test_roleplay_impression_separate_and_fuzzy_time(self):
        self.memory.apply(suggestion(kind="scene", mode="roleplay"), self.snap(), NOW)
        self.assertEqual(self.memory.status(NOW+6*3600)["entries"], 0)
        self.memory.apply(suggestion(source="m2", kind="impression"), self.snap(2), NOW)
        labels = {"wxid_a": "U1"}
        text = self.memory.prompt(labels, "wxid_a", NOW)
        self.assertIn("暂时推测", text)
        self.assertIn('"mode": "roleplay"', text)
        self.assertEqual(fuzzy_time(NOW, NOW), "今天")
        self.assertEqual(fuzzy_time(NOW, NOW+DAY), "昨天")
        self.assertEqual(fuzzy_time(NOW, NOW+3*DAY), "这几天")
        self.assertEqual(fuzzy_time(NOW, NOW+9*DAY), "上周")
        self.assertEqual(fuzzy_time(NOW, NOW+20*DAY), "前阵子")

    def test_forget_watermark_old_window_and_stale_task_cannot_relearn(self):
        self.memory.apply(suggestion(), self.snap(), NOW)
        pending = self.snap(2)
        self.memory.maintain("forget", member="wxid_a", watermark=2, now=NOW)
        with self.assertRaises(ValueError):
            self.memory.apply(suggestion(source="m2"), pending, NOW)
        self.assertEqual(self.snap(2)["messages"], [])
        self.memory.apply(dict(updates=[]), self.snap(2), NOW)
        self.assertEqual(self.memory.status(NOW)["members"], 0)

    def test_correct_does_not_renew_and_json_is_authority(self):
        self.memory.apply(suggestion(), self.snap(), NOW)
        entry = self.memory.data["members"]["wxid_a"]["entries"][0]
        self.memory.maintain("correct", member="wxid_a", entry=entry["id"], text="喜欢自然回答", now=NOW+DAY)
        loaded = MemoryStore(self.temp.name, IDENTITY)
        self.assertEqual(loaded.data["members"]["wxid_a"]["entries"][0]["confirmed_at"], NOW)
        self.assertNotIn("我喜欢简洁直接的回复", loaded.path.read_text(encoding="utf-8"))

    def test_context_members_bot_exclusion_references_not_evidence(self):
        a, b, bot = msg(1, NOW-1, "你好", "wxid_b"), msg(2, NOW, "@bot 好", "wxid_a"), msg(3, NOW, "虚构故事", "wxid_bot")
        b["reference"] = dict(sender="wxid_unknown", text="第三方转述", kind="text")
        value = snapshot(self.cfg, [a, b, bot], [b], IDENTITY, self.memory, NOW)
        self.assertEqual(set(value["members"]), {"wxid_a", "wxid_b"})
        self.assertNotIn("第三方转述", json.dumps(value, ensure_ascii=False))
        self.assertNotIn("虚构故事", json.dumps(value, ensure_ascii=False))
        proposal = suggestion(source="m1")
        with self.assertRaises(ValueError):
            self.memory.apply(proposal, value, NOW)

    def test_prompt_budget_labels_cold_natural_and_no_history_truncation(self):
        self.memory.apply(suggestion(), self.snap(), NOW)
        self.memory.data["members"]["wxid_a"]["affinity"] = 10
        a, b = msg(1, NOW-1, "完整历史", "wxid_b"), msg(2, NOW, "提问", "wxid_a")
        a["sender_name"] = b["sender_name"] = "同名"
        labels = member_labels([a], [b], "wxid_bot")
        extra = self.memory.prompt(labels, "wxid_a", NOW)
        self.assertIn("避免固定话术", extra)
        self.assertLessEqual(len(extra), 1200)
        prompt = build_prompt(self.cfg, [a], b, "测试", memory=dict(labels=labels, text=extra))[1]["content"]
        self.assertIn("U1（同名）", prompt)
        self.assertIn("U2（同名）", prompt)
        self.cfg["target"]["context_max_chars"] = len(build_prompt(self.cfg, [a], b, "测试", memory=dict(labels=labels))[1]["content"])
        short = build_prompt(self.cfg, [a], b, "测试", memory=dict(labels=labels, text=extra))[1]["content"]
        self.assertIn("完整历史", short)
        self.assertNotIn("以下人物记忆", short)

    def test_extractor_uses_memory_thinking_preserves_foreground_and_uses_no_tools(self):
        self.cfg["deepseek"]["reasoning_effort"] = "high"
        self.cfg["memory"].update(thinking="enabled", reasoning_effort="max")
        before = copy.deepcopy(self.cfg)
        with patch("wxbridge.ai.client.DeepSeekClient") as client:
            client.return_value.complete.return_value = dict(content='{"updates":[]}', reasoning_content="PRIVATE_REASONING")
            self.assertEqual(extract(self.cfg, self.snap()), dict(updates=[]))
            config = client.call_args.args[0]
            self.assertEqual(client.call_args.kwargs['role'], 'memory')
            from wxbridge.ai.roles import model_settings
            settings = model_settings(config, 'memory')
            self.assertEqual(settings['thinking'], 'enabled')
            self.assertEqual(settings['reasoning_effort'], 'max')
            self.assertEqual(self.cfg["deepseek"]["reasoning_effort"], "high")
            self.assertEqual(self.cfg, before)
            self.assertNotIn("tools", client.return_value.complete.call_args.kwargs)

    def test_memory_thinking_switch_and_effort_reach_api_payload(self):
        from wxbridge.ai.client import DeepSeekClient
        for thinking, effort in (("enabled", "low"), ("enabled", "high"), ("enabled", "max"), ("disabled", "max")):
            with self.subTest(thinking=thinking, effort=effort):
                self.cfg["memory"].update(thinking=thinking, reasoning_effort=effort)
                with patch("wxbridge.ai.client.deepseek_api_key", return_value="synthetic-key"), \
                        patch.object(DeepSeekClient, "_post", return_value={"choices": [{"message": {"content": '{"updates":[]}'}}]}) as post:
                    self.assertEqual(extract(self.cfg, self.snap()), dict(updates=[]))
                payload = post.call_args.args[0]
                self.assertEqual(payload["thinking"], {"type": thinking})
                if thinking == "enabled":
                    self.assertEqual(payload["reasoning_effort"], effort)
                else:
                    self.assertNotIn("reasoning_effort", payload)
                self.assertNotIn("tools", payload)

    def test_image_prompt_rebuild_preserves_memory_and_reference_author_label(self):
        from wxbridge.ai.images import prepare_multimodal
        a = msg(1, NOW, "这怎么看", "wxid_a")
        a["reference"] = dict(sender="wxid_b", sender_name="同名", kind="image", available=False, text="")
        memory = dict(labels={"wxid_a": "U1", "wxid_b": "U2"}, text="U1 喜欢直接回答")
        prompt = build_prompt(self.cfg, [], a, "测试", memory=memory)
        rebuilt, metadata = prepare_multimodal(self.cfg, prompt, dict(context=[], triggers=[a], chat_title="测试",
            images=[dict(trigger_index=0, depth=0, error="local_image_not_cached")], memory=memory))
        self.assertIn("U1 喜欢直接回答", rebuilt[1]["content"])
        self.assertIn("U2（同名）", rebuilt[1]["content"])

    @unittest.skipIf(os.name == "nt", "Linux 文件权限与 flock")
    def test_private_permissions_and_maintenance_listener_lock(self):
        from argparse import Namespace
        from wxbridge.cli import memory_command
        from wxbridge.app.runtime import instance_lock
        self.memory.apply(suggestion(), self.snap(), NOW)
        for path, expected in [(self.memory.directory, 0o700), (self.memory.path, 0o600), (self.memory.projection, 0o600)]:
            self.assertEqual(path.stat().st_mode & 0o777, expected)
        self.cfg["memory"]["directory"] = self.temp.name
        self.cfg["queue"]["file"] = str(Path(self.temp.name)/"queue.sqlite3")
        self.cfg["decrypted_dir"] = str(Path(self.temp.name)/"decrypted")
        Path(self.cfg["decrypted_dir"]).mkdir(mode=0o700)
        with patch("wxbridge.app.runtime.checked_path", side_effect=lambda p: Path(p)):
            with instance_lock(self.cfg["queue"]["file"]+".lock"):
                with self.assertRaisesRegex(ValueError, "已有监听实例"):
                    memory_command(self.cfg, Namespace(action="clear"))
        os.chmod(self.memory.path, 0o644)
        with self.assertRaisesRegex(ValueError, "memory_file_permissions"):
            MemoryStore(self.temp.name, IDENTITY)

    def test_pruning_removes_expired_disk_content_without_api_or_relearning(self):
        self.memory.apply(suggestion(kind="scene"), self.snap(), NOW)
        self.assertTrue(self.memory.prune(NOW+6*3600))
        self.assertEqual(MemoryStore(self.temp.name, IDENTITY).status(NOW+6*3600)["entries"], 0)
        self.assertEqual(self.memory.data["watermark"], 1)
        self.assertFalse(self.memory.prune(NOW+6*3600))

    def test_json_write_failure_keeps_watermark_for_retry_projection_failure_commits_once(self):
        from wxbridge.storage.person_memory import atomic_write
        value = self.snap()
        with patch("wxbridge.storage.person_memory.atomic_write", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.memory.apply(suggestion(delta=3), value, NOW)
        self.assertEqual(self.memory.status(NOW)["watermark"], 0)
        self.assertEqual(self.memory.status(NOW)["revision"], 0)
        def fail_projection(path, text):
            if path.name == "additional_prompt.md":
                raise OSError("synthetic")
            return atomic_write(path, text)
        with patch("wxbridge.storage.person_memory.atomic_write", side_effect=fail_projection):
            self.memory.apply(suggestion(delta=3), value, NOW)
        self.assertEqual(MemoryStore(self.temp.name, IDENTITY).data["members"]["wxid_a"]["affinity"], 53)
        self.assertEqual(self.memory.status(NOW)["watermark"], 1)

    def test_sensitive_model_summary_never_persists(self):
        value = suggestion()
        value["updates"][0]["memories"][0]["summary"] = "sk-"+"a"*32
        with self.assertRaisesRegex(ValueError, "memory_sensitive_text"):
            self.memory.apply(value, self.snap(), NOW)
        self.assertFalse(self.memory.path.exists())

    def test_late_synced_older_message_above_trigger_watermark_does_not_score_twice(self):
        trigger = msg(2, NOW, "提问", "wxid_a")
        late = msg(4, NOW-5, "谢谢你，辛苦了", "wxid_a")
        first = snapshot(self.cfg, [trigger, late], [trigger], IDENTITY, self.memory, NOW)
        self.memory.apply(suggestion(source="m4", delta=3), first, NOW)
        self.assertEqual(self.memory.data["watermark"], 2)
        self.assertEqual(self.memory.data["processed_ids"], [4])
        next_trigger = msg(3, NOW+1, "下一个问题", "wxid_a")
        second = snapshot(self.cfg, [trigger, late, next_trigger], [next_trigger], IDENTITY, self.memory, NOW+1)
        self.assertEqual([m["local_id"] for m in second["messages"]], [3])
        with self.assertRaises(ValueError):
            self.memory.apply(suggestion(source="m4", delta=3), second, NOW+1)
        self.memory.apply(dict(updates=[]), second, NOW+1)
        self.assertEqual(self.memory.data["members"]["wxid_a"]["affinity"], 53)


class MemoryPipelineTests(PipelineTests):
    # 不重复父类测试；此类通过自定义 load_tests 仅选择新增用例。
    def enable(self):
        self.bot.close()
        self.cfg["memory"].update(enabled=True, directory=str(Path(self.temp.name)/"memory"))
        self.cfg["queue"]["debounce_seconds"] = 0
        self.make_bot()

    def confirmed(self):
        self.add(1)
        self.bot.tick()
        self.generated()
        self.add(2, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()

    def test_memory_after_final_confirmation_preempt_restart_and_recall(self):
        self.enable()
        self.add(0, sender="wxid_other", text="我也喜欢简洁", mention=False)
        self.confirmed()
        job = self.jobs[-1]
        self.assertEqual(job.kind, "memory")
        self.assertEqual(self.bot.journal.status()["memory_pending"], 1)
        self.add(3)
        self.bot.tick()
        self.assertEqual(len(self.bot.generators), 1)
        self.add(4)
        self.bot.tick()
        self.assertTrue(job.closed)
        self.assertIsNone(self.bot.memory_job)
        self.assertEqual(self.bot.journal.memory_jobs()[0]["state"], "pending")
        self.generated()
        job = next(j for j in reversed(self.jobs) if j.kind == "memory")
        job.result = dict(ok=True, proposal=suggestion())
        self.bot.tick()
        self.assertEqual(self.bot.memory.status(self.now)["revision"], 2)  # 启用学习基线与首次整理各一次修订。
        self.restart()
        self.add(5)
        self.bot.tick()
        prompt = next(j for j in reversed(self.jobs) if j.kind == "generate").payload[1][1]["content"]
        self.assertIn("喜欢简洁直接的答复", prompt)

    def test_memory_failure_does_not_block_receive_and_retries_safely(self):
        self.enable()
        self.confirmed()
        self.jobs[-1].result = dict(ok=False, error="SecretError")
        self.bot.tick()
        self.assertEqual(self.bot.journal.get("memory_error"), "memory_worker_failed")
        self.add(3)
        self.bot.tick()
        self.assertEqual(len(self.bot.generators), 1)
        self.assertNotIn("合成回复", self.bot.status_path.read_text(encoding="utf-8"))
        self.restart()
        self.assertEqual(self.bot.journal.status()["memory_pending"], 1)

    def test_notice_and_dry_run_do_not_enqueue_memory(self):
        self.cfg['search']['feedback_enabled'] = True
        self.enable()
        self.add(1)
        self.bot.tick()
        job = self.jobs[-1]
        job.result = dict(event="search_started")
        self.bot.tick()
        self.add(2, sender="wxid_self", text=self.sends()[-1].payload[2], mention=False)
        self.bot.tick()
        self.assertEqual(self.bot.journal.memory_jobs(), [])

    def test_restart_after_disk_commit_does_not_repeat_score_or_api(self):
        self.enable()
        self.confirmed()
        record, value, job = self.bot.memory_job
        self.bot.memory.apply(suggestion(delta=3), value, self.now)
        # 模拟 JSON 已持久化而队列完成事务尚未提交时崩溃。
        self.restart()
        count = len([j for j in self.jobs if j.kind == "memory"])
        self.bot.tick()
        self.assertEqual(len([j for j in self.jobs if j.kind == "memory"]), count)
        self.assertEqual(self.bot.journal.memory_jobs(), [])
        self.assertEqual(self.bot.memory.data["members"]["wxid_friend"]["affinity"], 53)

    def test_background_minimum_interval_foreground_priority_and_recovery(self):
        self.enable()
        self.confirmed()
        self.jobs[-1].result = dict(ok=True, proposal=suggestion())
        self.bot.tick()
        self.add(3)
        self.bot.tick()
        self.generated("第二次回复")
        # 前一次 UI 进程尚未退出时必须等待；交出其结果才能发送下一条。
        if self.bot.ui_job:
            self.bot.ui_job[2].result = dict(ok=True, ui=dict(submitted=True))
        self.bot.tick()
        self.add(4, sender="wxid_self", text="第二次回复", mention=False)
        self.bot.tick()
        self.assertIsNone(self.bot.memory_job)
        self.assertEqual(len(self.bot.journal.memory_jobs()), 1)
        self.now += 60
        self.bot.tick()
        self.assertIsNotNone(self.bot.memory_job)
        self.restart()
        self.bot.tick()
        self.assertIsNotNone(self.bot.memory_job)
        self.assertEqual(self.bot.memory_job[0]["state"], "running")

    def test_forgotten_pending_task_skips_before_missing_database_source(self):
        self.enable()
        self.confirmed()
        self.bot.close()
        self.bot.memory.maintain("clear", watermark=2, now=self.now)
        self.rows = []
        self.make_bot()
        self.store.messages_in_window.reset_mock()
        self.bot._memory_step()
        self.store.messages_in_window.assert_not_called()
        self.assertEqual(self.bot.journal.memory_jobs(), [])

    def test_two_reply_bubbles_confirm_in_order_and_enqueue_memory_only_after_second(self):
        self.enable()
        first, second = '先检查连接地址和端口，确认没有填错。', '再检查服务监听状态，看看是不是只绑定了本地。'
        self.add(1)
        self.bot.tick()
        self.generated(first+'\n\n'+second)
        self.assertEqual(self.sends()[0].payload[2], first)
        self.sends()[0].result = dict(ok=True, ui=dict(submitted=True))
        self.add(2, sender="wxid_self", text=first, mention=False)
        self.bot.tick()
        self.assertEqual(self.sends()[-1].payload[2], second)
        self.assertEqual(self.bot.journal.memory_jobs(), [])
        self.add(3, sender="wxid_self", text=second, mention=False)
        self.bot.tick()
        self.assertEqual(len(self.bot.journal.memory_jobs()), 1)
        self.assertEqual(self.bot.journal.task(1)["phase"], "done")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(MemoryTests))
    for name in MemoryPipelineTests.__dict__:
        if name.startswith("test_"):
            suite.addTest(MemoryPipelineTests(name))
    return suite
