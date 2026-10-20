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
