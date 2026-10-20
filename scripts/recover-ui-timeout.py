"""人工故障处理入口：修复 UI 后单次恢复；不由监听自动调用。"""
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
    if sys.argv[1:] != ["--controlled-recovery-task-3"]:
        raise ValueError("本脚本只用于本轮已授权故障修复，不是通用重试命令")
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root/"config.json")
    with ExitStack() as stack:
        for lock in sorted({cfg["queue"]["file"]+".lock", str(Path(cfg["decrypted_dir"])/".listener.lock")}):
            stack.enter_context(instance_lock(lock))
        bot = WeChatDeepSeekBot(cfg)
        try:
            task = bot.journal.head()
            if not task or task["id"] != 3 or not task["intent"] or task.get("recovery"):
                raise ValueError("不符合本轮受控恢复的任务，保持原状态")
            intent = task["intent"]
            if (intent.get("ui") or {}).get("error") != "ui_error":
                raise ValueError("与本轮旧版本 UI 故障记录不符")
            if time.time()-intent["created_at"] < 30:
                raise ValueError("尚未给迟到发出记录留足时间")
            bot._confirm_step()
            task = bot.journal.task(3)
            if not task["intent"]:
                print(json.dumps({"recovery": "late_outbound_confirmed"}))
                return
            a = A11y(cfg["ui"])
            if a.chat_is_selected(bot.chat_title) is not True:
                raise ValueError("目标群未选中")
            editor, _ = a.composer_controls()
            expected = task["chunks"][task["next_chunk"]]
            if a.read_text(editor) != expected:
                raise ValueError("草稿与原定回复不同，保留草稿和任务")
            with bot.journal.db:
                task.update(intent=None, blocked_reason="",
                            recovery={"reason": "controlled_recovery_after_ui_sync_fix", "at": time.time(),
                                      "old_baseline": intent["baseline"]})
                bot.journal.put(task)
            print(json.dumps({"recovery": "controlled_once", "task": 3, "draft_chars": len(expected),
                              "outbound_absent": True, "draft_matches": True}), flush=True)
        finally:
            bot.close()


if __name__ == "__main__":
    main()
