"""检查与排障命令：与正常监听分开，不维护另一套消息接收循环。"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from provider import load_config
from wxbridge.wechat.store import StoreError, WeChatStore, discover_db_dir
from wxbridge.wechat.ui import A11y, Sender, UIError


def check(cfg: dict, group: str = "") -> int:
    from contextlib import ExitStack
    from wxbridge.app.runtime import instance_lock
    from wxbridge.instances import runtime_locks
    with ExitStack() as stack:
        for path in runtime_locks(cfg):
            stack.enter_context(instance_lock(path))
        return _check_locked(cfg, group)


def _check_locked(cfg: dict, group: str = "") -> int:
    store = WeChatStore(cfg["db_dir"], cfg["keys_file"], cfg["decrypted_dir"])
    print(store.describe())
    chat_id, title = store.resolve_chat(group or cfg["target"]["chat"])
    print(f"目标群：{title} ({chat_id})")
    print(f"最新消息 local_id：{store.last_local_id(chat_id)}（不显示正文）")
    if cfg['ui']['backend']=='native_service':
        from wxbridge.native.sender import readiness
        print(json.dumps(readiness(cfg)))
    else:
        print(json.dumps(Sender(cfg).status(), ensure_ascii=False, indent=2))
    print(f"模型连接密钥：{'已配置' if load_config(cfg.get('provider_env')).api_key(cfg['model_clients']['reply']) else '未配置'}")
    return 0


def environment(cfg: dict) -> int:
    print(f"DISPLAY={os.environ.get('DISPLAY', '')}")
    for tool in ("Xvfb", "dbus-launch", "xdotool", "x11vnc"):
        print(f"{tool}: {shutil.which(tool) or '未找到'}")
    print(f"消息来源：{cfg.get('source')}")
    print(f"密钥文件存在：{Path(cfg['keys_file']).is_file()}")
    try:
        print(f"数据库目录：{discover_db_dir(cfg.get('db_dir') or '')}")
    except StoreError as exc:
        print(f"数据库目录尚不可用：{exc}")
    return 0


def ui_tree(cfg: dict, scope: str, depth: int) -> int:
    a11y = A11y(cfg["ui"])
    if not a11y.available:
        raise UIError(f"AT-SPI 不可用：{a11y.error}")
    result = a11y.dump(scope, max_depth=depth)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


def input_test(cfg: dict) -> int:
    result = A11y(cfg['ui']).input_test()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0
