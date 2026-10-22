"""合成头像、内存 SQLite、假 API；头像不能作为人格或好感证据。"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbridge.ai.avatars import classify, validate_visual
from wxbridge.ai.memory import extract, snapshot
from wxbridge.config import DEFAULTS, _deep_merge
from wxbridge.storage.person_memory import MemoryStore, DAY
from wxbridge.wechat.store import WeChatStore
from test_media import png
from test_memory import IDENTITY
from test_context_pipeline import PipelineTests
from synthetic import NOW, msg

VISUAL = dict(subject="animal", medium="illustration", palette="warm", composition="simple")


def binding():
    data = png()
    return dict(available=True, data=data, mime="image/png", digest=hashlib.sha256(data).hexdigest(),
                width=2, height=1, observed_at=NOW)


def update(label="U1", digest=None):
    return dict(member=label, digest=digest or binding()["digest"], visual=dict(VISUAL))


class AvatarTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.memory = MemoryStore(self.temp.name, IDENTITY)
        self.cfg = _deep_merge(DEFAULTS, {})
        self.message = msg(1, NOW, "你好", "wxid_a")

    def snap(self, number=1):
        message = dict(self.message, local_id=number)
        value = snapshot(self.cfg, [message], [message], IDENTITY, self.memory, NOW)
        value["avatars"] = {"wxid_a": binding()}
        return value

    def test_exact_username_binding_and_missing_cache_not_arbitrary_neighbor(self):
        def connection():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE head_image(username TEXT,md5 TEXT,image_buffer BLOB,update_time INTEGER)")
            conn.execute("INSERT INTO head_image VALUES('wxid_a','same-name',?,1)", (png(),))
            conn.execute("INSERT INTO head_image VALUES('wxid_b','same-name',?,1)", (png(3),))
            return conn
        store = WeChatStore.__new__(WeChatStore)
        store._connect = Mock(side_effect=lambda _: connection())
        a, b = store.avatar("wxid_a"), store.avatar("wxid_b")
        self.assertTrue(a["available"])
        self.assertNotEqual(a["digest"], b["digest"])
        self.assertEqual(store.avatar("wxid_unknown")["error"], "avatar_not_cached")
        store._connect.assert_called_with("head_image/head_image.db")
        self.assertEqual(store.avatar("x@chatroom")["error"], "avatar_invalid_member")

    def test_oversize_invalid_and_ambiguous_avatar_are_unavailable(self):
        for blobs, limit, error in [([b"invalid"], 1000, "avatar_unavailable"), ([png()], 5, "avatar_size_limit"),
                                    ([png(), png(3)], 1000, "avatar_ambiguous")]:
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE head_image(username TEXT,image_buffer BLOB,update_time INTEGER)")
            conn.executemany("INSERT INTO head_image VALUES('wxid_a',?,1)", [(b,) for b in blobs])
            store = WeChatStore.__new__(WeChatStore)
            store._connect = Mock(return_value=conn)
            self.assertEqual(store.avatar("wxid_a", max_bytes=limit)["error"], error)

    def test_visual_enums_reject_personality_and_extra_free_text(self):
        self.assertEqual(validate_visual(VISUAL), VISUAL)
        for value in [dict(VISUAL, personality="外向"), dict(VISUAL, subject="高冷的人"), dict(VISUAL, palette="年龄20")]:
            with self.assertRaises(ValueError):
                validate_visual(value)

    def test_text_judgement_never_sees_avatar_and_visual_has_no_chat_context(self):
        value = self.snap()
        response = dict(avatars=[update()])
        with patch("wxbridge.ai.client.DeepSeekClient") as client:
            client.return_value.complete.side_effect = [dict(content='{"updates":[]}'), dict(content=json.dumps(response))]
            result = extract(self.cfg, value)
            first, second = client.return_value.complete.call_args_list
            self.assertNotIn("image_url", json.dumps(first.args, ensure_ascii=False))
            self.assertNotIn("data:image", json.dumps(first.args, ensure_ascii=False))
            self.assertIn("data:image/png;base64,", json.dumps(second.args))
            self.assertNotIn("你好", json.dumps(second.args, ensure_ascii=False))
            self.assertEqual(result["avatar_updates"], [update()])
            self.assertEqual(client.call_args.kwargs['role'], 'avatar')
            from wxbridge.ai.roles import model_settings
            self.assertEqual(model_settings(client.call_args.args[0], 'avatar')['thinking'], 'disabled')

    def test_visual_api_failure_keeps_text_proposal(self):
        with patch("wxbridge.ai.client.DeepSeekClient") as client:
            client.return_value.complete.side_effect = [dict(content='{"updates":[]}'), TimeoutError()]
            result = extract(self.cfg, self.snap())
        self.assertEqual(result, dict(updates=[], avatar_updates=[]))
        self.memory.apply(result, self.snap(), NOW)
        self.assertEqual(self.memory.data["members"]["wxid_a"]["initial_impression"]["state"], "unavailable")
        self.assertEqual(self.memory.data["watermark"], 1)

    def test_initial_visual_is_private_weak_and_does_not_change_affinity(self):
        self.memory.apply(dict(updates=[], avatar_updates=[update()]), self.snap(), NOW)
        person = self.memory.data["members"]["wxid_a"]
        self.assertEqual(person["affinity"], 50)
        self.assertEqual(person["initial_impression"]["confidence"], .2)
        text = self.memory.prompt({"wxid_a": "U1"}, "wxid_a", NOW)
        self.assertIn("不主动提头像", text)
        self.assertIn("动物元素", text)
        self.assertNotIn("初见视觉", self.memory.prompt({"wxid_a": "U1"}, "wxid_a", NOW, use_avatar=False))
        saved = self.memory.path.read_text(encoding="utf-8")
        self.assertNotIn("base64", saved)
        self.assertNotIn("image/png", saved)
        self.assertNotIn("你好", saved)
        self.assertEqual(MemoryStore(self.temp.name, IDENTITY).data, self.memory.data)
        self.assertNotIn("initial_impression", json.dumps(self.memory.background({"wxid_a": "U1"}, NOW)))

    def test_unknown_member_or_digest_never_writes_initial_but_text_advances(self):
        for item in [update("U999"), update(digest="a"*64), dict(update(), personality="温柔")]:
            with tempfile.TemporaryDirectory(dir=self.temp.name) as temp:
                memory = MemoryStore(temp, IDENTITY)
                value = self.snap()
                memory.apply(dict(updates=[], avatar_updates=[item]), value, NOW)
                self.assertEqual(memory.data["watermark"], 1)
                self.assertNotEqual(memory.data["members"]["wxid_a"]["initial_impression"]["state"], "ready")

    def test_dialogue_impressions_override_visual_and_early_visual_fades(self):
        self.memory.apply(dict(updates=[], avatar_updates=[update()]), self.snap(), NOW)
        labels = {"wxid_a": "U1"}
        person = self.memory.data["members"]["wxid_a"]
        person["entries"].append(dict(id="guess", kind="impression", mode="real", summary="暂时感觉说话直接",
            sources=[1], confidence=.7, occurred_at=NOW, confirmed_at=NOW))
        self.assertNotIn("初见视觉", self.memory.prompt(labels, "wxid_a", NOW))
        person["entries"] = []
        person["interaction_batches"] = 4
        self.assertNotIn("初见视觉", self.memory.prompt(labels, "wxid_a", NOW))
        person["interaction_batches"] = 1
        self.assertNotIn("初见视觉", self.memory.prompt(labels, "wxid_a", NOW+30*DAY))
        self.memory.prune(NOW+90*DAY)
        self.assertEqual(person["initial_impression"]["state"], "ready")  # prune 原子替换对象。
        self.assertEqual(self.memory.data["members"]["wxid_a"]["initial_impression"]["state"], "expired")

    def test_forgetting_initial_blocks_old_snapshot_and_recreation(self):
        self.memory.apply(dict(updates=[], avatar_updates=[update()]), self.snap(), NOW)
        pending = self.snap(2)
        self.memory.maintain("forget", member="wxid_a", entry="initial", watermark=2, now=NOW)
        with self.assertRaises(ValueError):
            self.memory.apply(dict(updates=[], avatar_updates=[update()]), pending, NOW)
        self.memory.apply(dict(updates=[], avatar_updates=[update()]), self.snap(3), NOW)
        self.assertEqual(self.memory.data["members"]["wxid_a"]["initial_impression"]["state"], "forgotten")


class AvatarPipelineTests(PipelineTests):
    def test_avatars_only_after_confirm_and_known_initial_not_requested_again(self):
        self.bot.close()
        self.cfg["memory"].update(enabled=True, directory=str(Path(self.temp.name)/"memory"))
        self.cfg["queue"]["debounce_seconds"] = 0
        self.store.avatar.return_value = binding()
        self.make_bot()
        self.add(1)
        self.bot.tick()
        self.store.avatar.assert_not_called()
        self.generated()
        self.add(2, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.store.avatar.assert_called_once_with("wxid_friend", max_bytes=524288)
        self.jobs[-1].result = dict(ok=True, proposal=dict(updates=[], avatar_updates=[update()]))
        self.bot.tick()
        self.assertEqual(self.bot.memory.status(self.now)["initial_impressions"], 1)
        self.now += 60
        self.add(3)
        self.bot.tick()
        self.generated("第二次")
        if self.bot.ui_job:
            self.bot.ui_job[2].result = dict(ok=True, ui=dict(submitted=True))
        self.bot.tick()
        self.add(4, sender="wxid_self", text="第二次", mention=False)
        self.bot.tick()
        self.assertEqual(self.store.avatar.call_count, 1)

    def test_priority_limit_and_later_fill_for_old_related_members(self):
        self.bot.close()
        self.cfg["memory"].update(enabled=True, directory=str(Path(self.temp.name)/"memory"))
        self.cfg["queue"]["debounce_seconds"] = 0
        self.store.avatar.return_value = binding()
        self.make_bot()
        self.add(1, sender="wxid_old_a", mention=False)
        self.add(2, sender="wxid_old_b", mention=False)
        self.add(3)
        self.bot.tick()
        self.generated()
        self.add(4, sender="wxid_self", text="合成回复", mention=False)
        self.bot.tick()
        self.assertEqual([c.args[0] for c in self.store.avatar.call_args_list], ["wxid_friend", "wxid_old_a"])
        self.jobs[-1].result = dict(ok=True, proposal=dict(updates=[], avatar_updates=[update("U1"), update("U2")]))
        self.bot.tick()
        self.now += 60
        self.add(5)
        if self.bot.ui_job:
            self.bot.ui_job[2].result = dict(ok=True, ui=dict(submitted=True))
        self.bot.tick()
        self.generated("第二次")
        self.add(6, sender="wxid_self", text="第二次", mention=False)
        self.bot.tick()
        self.assertEqual(self.store.avatar.call_args.args[0], "wxid_old_b")


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(AvatarTests)
    for name in AvatarPipelineTests.__dict__:
        if name.startswith("test_"):
            suite.addTest(AvatarPipelineTests(name))
    return suite
