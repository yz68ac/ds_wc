"""生产入口：规则、单实例锁及可恢复队列生命周期。"""
from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path

from wxbridge.app.bot import WeChatDeepSeekBot, setup_logging
from wxbridge.storage.journal import checked_path


@contextmanager
def instance_lock(path):
    import fcntl
    target = checked_path(path)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("已有监听实例占用队列或解密目录") from exc
        yield
    finally:
        os.close(fd)


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
