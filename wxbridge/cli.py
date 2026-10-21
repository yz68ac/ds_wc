"""命令行分发：检查 → 密钥准备 → 统一监听；额外 UI 排障按需使用。"""
from __future__ import annotations

import argparse
import json

from wxbridge import __version__
from wxbridge.app import diagnostics
from wxbridge.config import load_config
from wxbridge.wechat.keyscan import extract_keys
from wxbridge.app.runtime import run_bot
from wxbridge.storage.journal import read_status
from wxbridge.wechat.store import discover_db_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxbridge", description="Linux 微信 + DeepSeek 群聊桥接")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env", help="检查环境和依赖，不调用模型或发消息")
    check = sub.add_parser("check", help="检查数据库、目标群和发送组件，不显示聊天正文")
    check.add_argument("--group", default="")
    scan = sub.add_parser("scan-keys", help="从已登录微信的本机进程提取并验证数据库密钥")
    scan.add_argument("--db-dir", default="")
    scan.add_argument("--out", default="")
    scan.add_argument("--pid", type=int, default=None)
    sub.add_parser("queue-status", help="只读队列元数据，不显示正文")
    memory = sub.add_parser("memory", help="人物记忆维护；修改前须停止监听")
    memory.add_argument("action", choices=["status", "show", "correct", "forget", "clear"])
    memory.add_argument("--member", help="稳定 wxid；show 不填则显示全部")
    memory.add_argument('--scope', choices=['member', 'group', 'bot'], help='人物记忆、群内用法或ds表达偏好')
    memory.add_argument("--entry", help="单条记忆 id；forget 不填则遗忘该成员")
    memory.add_argument("--text-file", help="correct 使用 UTF-8 文本文件，最多 220 字")
    run = sub.add_parser("run", help="启动监听，有内存队列则恢复，无队列时建立基线")
    sending = run.add_mutually_exclusive_group()
    sending.add_argument("--send", action="store_true", help="显式启用当前进程发送")
    sending.add_argument("--dry-run", action="store_true", help="不发送；仍可能调用模型")
    run.add_argument("--once", action="store_true", help="只调度一次，未完成任务保留到下次启动")
    run.add_argument("--echo", action="store_true", help="用固定 echo 代替模型，便于单独排查收发链路")
    tree = sub.add_parser("a11y", help="UI 排障：显式查看无障碍树，可能显示聊天正文")
    tree.add_argument("--scope", choices=["chats", "messages", "editables", "full", "desktop"], default="chats")
    tree.add_argument("--depth", type=int, default=8)
    sub.add_parser("input-test", help="UI 排障：在当前输入框写入测试文字，不点击发送")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "memory":
            return memory_command(cfg, args)
        if args.command == "queue-status":
            print(json.dumps(read_status(cfg["queue"]["file"]), ensure_ascii=False, indent=2))
            return 0
        if args.command == "env":
            return diagnostics.environment(cfg)
        if args.command == "check":
            return diagnostics.check(cfg, args.group)
        if args.command == "scan-keys":
            db_dir = args.db_dir or discover_db_dir(cfg.get("db_dir") or "")
            extract_keys(db_dir, args.out or cfg["keys_file"], pid=args.pid, show_keys=False)
            return 0
        if args.command == "run":
            if args.send:
                cfg["ui"]["dry_run"] = False
            elif args.dry_run:
                cfg["ui"]["dry_run"] = True
            return run_bot(cfg, once=args.once, echo=args.echo)
        if args.command == "a11y":
            return diagnostics.ui_tree(cfg, args.scope, args.depth)
        if args.command == "input-test":
            return diagnostics.input_test(cfg)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


def memory_command(cfg, args):
    from contextlib import ExitStack
    from pathlib import Path
    import sqlite3
    import time
    from wxbridge.app.runtime import instance_lock
    from wxbridge.storage.journal import checked_path
    from wxbridge.storage.person_memory import MemoryStore
    mutating = args.action not in ("status", "show")
    scope = getattr(args, 'scope', None)
    if scope in ('group', 'bot') and getattr(args, 'member', None):
        raise ValueError('group/bot 作用域不能同时指定 --member')
    with ExitStack() as stack:
        if mutating:
            for path in sorted({cfg["queue"]["file"]+".lock", str(Path(cfg["decrypted_dir"])/".listener.lock")}):
                stack.enter_context(instance_lock(path))
        path = checked_path(cfg["queue"]["file"], existing=True)
        db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
        try:
            meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta WHERE key IN ('identity','cursor')")}
        finally:
            db.close()
        store = MemoryStore(cfg["memory"]["directory"], meta["identity"], create=mutating)
        now = time.time()
        if args.action == "status":
            result = store.status(now)
        elif args.action == "show":
            if scope in ('group', 'bot'):
                result = [r for r in store.data.get('learning', {}).get('records', []) if r['scope'] == scope]
            elif args.member:
                result = {**store.data['members'].get(args.member, {}), 'expression_habits':
                          [r for r in store.data.get('learning', {}).get('records', []) if r['owner'] == args.member]}
            else:
                result = store.data['members'] if scope == 'member' else store.data
        else:
            text = Path(args.text_file).read_text(encoding="utf-8-sig") if args.text_file else None
            store.maintain(args.action, member=args.member, entry=args.entry, text=text,
                           watermark=meta["cursor"], now=now, scope=scope)
            result = store.status(now)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
