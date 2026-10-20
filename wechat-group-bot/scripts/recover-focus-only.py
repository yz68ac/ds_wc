"""一次性修复旧版把 SetFocus 当点击的未提交任务；不适用于任何真实点击超时。"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wxbridge.app.bot import WeChatDeepSeekBot
from wxbridge.config import load_config
from wxbridge.app.runtime import instance_lock
from wxbridge.wechat.ui import A11y


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root/"config.json")
    with ExitStack() as stack:
        for lock in sorted({cfg["queue"]["file"]+".lock", str(Path(cfg["decrypted_dir"])/".listener.lock")}):
            stack.enter_context(instance_lock(lock))
        bot = WeChatDeepSeekBot(cfg)
        try:
            task = bot.journal.head()
            if not task or not task["intent"]:
                print(json.dumps({"recovery": "no_unresolved_intent"}))
                return
            intent = task["intent"]
            expected = task["chunks"][intent["part"]]
            ui = intent.get("ui") or {}
            if (ui.get("action") != "atspi-action" or ui.get("before_chars") != len(expected)
                    or ui.get("after_chars") != len(expected) or task.get("recovery")):
                raise ValueError("不符合只聚焦旧缺陷的恢复条件，保留意图并停止")
            # 同一个主进程先核对 DB。若记录已晚到，只确认，不再次提交。
            bot._confirm_step()
            task = bot.journal.task(task["id"])
            if not task["intent"]:
                print(json.dumps({"recovery": "already_sent_confirmed", "task": task["id"]}))
                return
            a = A11y(cfg["ui"])
            if a.chat_is_selected(bot.chat_title) is not True:
                raise ValueError("目标群未选中，保留草稿和任务")
            editor, button = a.composer_controls()
            if a.read_text(editor) != expected:
                raise ValueError("草稿不再等于待发分片，保留并停止")
            action = button.get_action_iface()
            names = [a.Atspi.Action.get_action_name(action, i) for i in range(action.get_n_actions())]
            if not names or any(name != "SetFocus" for name in names):
                raise ValueError("无法证明旧动作只聚焦，禁止重置意图")
            with bot.journal.db:
                task.update(intent=None, blocked_reason="",
                            recovery={"reason": "verified_focus_only_no_submission", "at": time.time(),
                                      "previous_baseline": intent["baseline"], "part": intent["part"],
                                      "version": task["version"]})
                bot.journal.put(task)
            print(json.dumps({"recovery": "verified_focus_only_no_submission", "task": task["id"],
                              "version": task["version"], "draft_chars": len(expected),
                              "actions": names}), flush=True)
        finally:
            bot.close()


if __name__ == "__main__":
    main()
