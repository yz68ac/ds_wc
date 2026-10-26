"""生产入口：规则、单实例锁及可恢复队列生命周期。"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from pathlib import Path

from wxbridge.app.bot import WeChatDeepSeekBot, setup_logging
from wechat_linux.runtime.locking import instance_lock


def run_bot(cfg, *, once=False, echo=False):
    cfg = copy.deepcopy(cfg)
    if cfg.get("source") != "db":
        raise ValueError("当前运行链路只使用 db 消息源")
    cfg["target"]["trigger"].update(require_mention_metadata=True, reply_all=False, prefixes=[])
    cfg["ui"]["verify_chat_selected"] = True
    cfg["reply_to_backlog"] = False
    if cfg["ui"].get("dry_run"):
        cfg["queue"]["file"] += ".dry-run"
        cfg["state_file"] = str(Path(cfg["state_file"]).parent / "dry-run" / "state.json")
    with ExitStack() as stack:
        locks = {cfg["queue"]["file"] + ".lock", str(Path(cfg["decrypted_dir"]) / ".listener.lock")}
        for path in sorted(locks):
            stack.enter_context(instance_lock(path))
        setup_logging(cfg, verbose=False)
        bot = WeChatDeepSeekBot(cfg, echo=echo)
        try:
            bot.save_status()
            bot.run(once=once)
        finally:
            bot.close()
    return 0
