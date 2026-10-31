"""Shared synthetic pipeline fixtures; contains no executable tests."""
from __future__ import annotations
from tests.support.model_support import default_config
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from wxbridge.app.bot import GroupSession
from tests.support.synthetic import NOW, msg, history_page
from tests.support.paths import ROOT


class FakeJob:
    def __init__(self, kind, payload, **kwargs):
        self.kind, self.payload, self.result, self.closed = kind, payload, None, False

    def poll(self):
        return self.result

    def close(self):
        self.closed = True


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.cfg = default_config()
        # 旧链路用例每次 tick 显式模拟接收；实际轮询节流在 test_polling 单独验证。
        self.cfg['poll_interval_seconds'] = 0
        self.cfg["memory"]["enabled"] = False
        self.cfg["target"].update(chat="合成群", cooldown_seconds=0)
        self.cfg["ui"]["dry_run"] = False
        self.cfg["queue"]["file"] = str(Path(self.temp.name) / "queue.sqlite3")
        self.cfg["participation"]["debounce_seconds"] = 0
        self.cfg["state_file"] = str(Path(self.temp.name) / "state.json")

        self.cfg['system_prompt_file'] = str(ROOT/'prompts/system.md')
        self.now, self.rows, self.jobs, self.decision_jobs = float(NOW), [], [], []
        self.store = Mock()
        self.store.self_wxid = "wxid_self"
        self.store.resolve_chat.return_value = ("synthetic@chatroom", "合成群")
        self.store.last_local_id.side_effect = lambda chat: max([m["local_id"] for m in self.rows], default=0)
        self.store.messages_after.side_effect = lambda chat, cursor, limit: [m for m in self.rows if m["local_id"] > cursor][:limit]
        self.store.messages_in_window.side_effect = lambda chat, start, end: list(self.rows)
        self.store.messages_before.side_effect = lambda chat, **kw: history_page(self.rows, **kw)
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
        self.bot = GroupSession(self.cfg)
        def make(kind, payload, **kwargs):
            job = FakeJob(kind, payload, **kwargs)
            if kind == 'participation':
                source_ids = payload[2]['source_ids']
                rows = {m['local_id']: m for m in self.rows}
                selected = [identifier for identifier in source_ids if
                    self.store.self_wxid in (rows[identifier].get('mentioned_users') or [])
                    or (rows[identifier].get('pat') or {}).get('target') == self.store.self_wxid]
                job.result = dict(ok=True, decision=dict(reply_to=selected))
                self.decision_jobs.append(job)
            else:
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

    def ready(self, answer='合成回复'):
        self.add(1)
        self.bot.tick()
        for job in self.jobs:
            if job.kind == 'generate' and not job.closed:
                job.result = dict(ok=True, reply=answer)
        self.bot.replies.collect()
        self.now += 3

    def search_started(self):
        generator = next(job for job in self.jobs if job.kind == "generate" and not job.closed)
        generator.result = {"event": "search_started"}
        self.bot.tick()
        generator.result = None
        return generator
