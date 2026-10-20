"""配置加载：config.json + .env + 环境变量。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS: dict[str, Any] = {
    # 微信数据目录；留空则自动探测
    "db_dir": "",
    "keys_file": "/run/wxbridge-trial/keys.json",
    "decrypted_dir": "/run/wxbridge-trial/decrypted",
    "state_file": "/run/wxbridge-trial/state.json",
    "log_file": "/run/wxbridge-trial/bot.log",
    "wechat_process": "wechat",
    # db = 本次实际运行的消息源；需已验证的密钥和 tmpfs,noswap 解密目录。
    # UI 仅负责发送和排障；不再维护无可靠时间的备用接收循环。
    "source": "db",
    "poll_interval_seconds": 2.0,
    "reply_to_backlog": False,
    "queue": {
        "debounce_seconds": 0,
        "generation_concurrency": 2,
        "file": "/run/wxbridge-trial/queue.sqlite3",
    },
    "target": {
        "chat": "",
        "bot_name": "DeepSeek",
        "trigger": {
            "mentions": ["@DeepSeek", "@deepseek"],
            "prefixes": [],
            "reply_all": False,
            "require_mention_metadata": True,
        },
        "cooldown_seconds": 8,
        "max_message_age_seconds": 90,
        "context_window_seconds": 600,
        # 超过预算时取消回复，不悄悄丢掉窗口内的消息。
        "context_max_chars": 24000,
        "max_reply_chars": 1800,
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "thinking": "disabled",
        "temperature": 1.0,
        "timeout_seconds": 60,
        "max_tokens": 1200,
        "system_prompt_file": "prompts/system.md",
    },
    "vision": {
        "enabled": True,
        "detail": "original",
        "max_images": 4,
        "max_image_bytes": 8 * 1024 * 1024,
        "cache_wait_seconds": 3,
    },
    "ui": {
        "backend": "auto",  # auto | atspi | xdotool
        "app_names": ["wechat", "WeChat", "微信"],
        "chat_list_names": ["Chats", "聊天", "会话"],
        "messages_list_names": ["Messages", "消息"],
        "window_class": "wechat",
        "native_executable": "/opt/wechat/wechat",
        "window_names": ["微信", "WeChat"],
        "search_shortcut": "ctrl+f",
        "paste_shortcut": "ctrl+v",
        "select_all_shortcut": "ctrl+a",
        "send_key": "Return",
        # auto = AT-SPI EditableText → xdotool type → 剪贴板
        # atspi / xtype / clipboard 可强制指定（无桌面时推荐 atspi 或 xtype）
        "input_method": "auto",
        # 必须确认目标群选中；状态未知时停止，不以关闭检查来绕过问题。
        "verify_chat_selected": True,
        "post_send_wait": 0.8,
        "send_confirm_timeout_seconds": 10,
        "chat_open_wait": 1.0,
        "dry_run": True,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dotenv(path: str | Path | None = None) -> None:
    p = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    p = Path(path)
    # API 凭据始终从源码根目录加载，不随 --config 或当前工作目录改变。
    load_dotenv()
    data: dict[str, Any] = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    cfg = _deep_merge(DEFAULTS, data)
    # 旧配置只提供内联提示词时继续使用它；显式提供文件时以文件为准。
    deepseek_data = data.get("deepseek") or {}
    if "system_prompt" in deepseek_data and "system_prompt_file" not in deepseek_data:
        cfg["deepseek"]["system_prompt_file"] = ""

    # 环境变量覆盖常用项
    if os.environ.get("WX_DB_DIR"):
        cfg["db_dir"] = os.environ["WX_DB_DIR"]
    if os.environ.get("DEEPSEEK_BASE_URL"):
        cfg["deepseek"]["base_url"] = os.environ["DEEPSEEK_BASE_URL"]
    if os.environ.get("DEEPSEEK_MODEL"):
        cfg["deepseek"]["model"] = os.environ["DEEPSEEK_MODEL"]
    if os.environ.get("WX_GROUP"):
        cfg["target"]["chat"] = os.environ["WX_GROUP"]

    # 统一转成绝对路径（相对于配置文件所在目录）
    base = p.resolve().parent
    for key in ("keys_file", "decrypted_dir", "state_file", "log_file"):
        value = str(cfg.get(key) or "")
        if value and not os.path.isabs(value):
            cfg[key] = str((base / value).resolve())
    prompt_file = cfg["deepseek"].get("system_prompt_file")
    if prompt_file:
        prompt_path = Path(prompt_file).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = base / prompt_path
        cfg["deepseek"]["system_prompt_file"] = str(prompt_path.resolve())
    queue_path = Path(cfg["queue"]["file"]).expanduser()
    if not queue_path.is_absolute():
        queue_path = base / queue_path
    cfg["queue"]["file"] = str(queue_path.resolve())
    if float(cfg["queue"]["debounce_seconds"]) < 0:
        raise ValueError("queue.debounce_seconds 必须大于或等于 0（0 表示每次 @ 独立排队）")
    if float(cfg["ui"]["send_confirm_timeout_seconds"]) <= 0:
        raise ValueError("ui.send_confirm_timeout_seconds 必须大于 0")
    concurrency = cfg["queue"]["generation_concurrency"]
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("queue.generation_concurrency 必须为正整数")
    vision = cfg["vision"]
    if not isinstance(vision["enabled"], bool) or vision["detail"] not in ("original", "high", "low", "auto"):
        raise ValueError("vision.enabled 必须为布尔值，detail 必须为 original/high/low/auto")
    for key, maximum in (("max_images", 4), ("max_image_bytes", 8 * 1024 * 1024)):
        value = vision[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"vision.{key} 必须为 1 到 {maximum} 的整数")
    if not 0 <= float(vision["cache_wait_seconds"]) <= 10:
        raise ValueError("vision.cache_wait_seconds 必须为 0 到 10 秒")
    return cfg


def deepseek_api_key(cfg: dict[str, Any]) -> str:
    env_name = cfg["deepseek"].get("api_key_env") or "DEEPSEEK_API_KEY"
    return os.environ.get(env_name, "").strip()
