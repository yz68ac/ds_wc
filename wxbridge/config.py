"""配置加载与校验：config.defaults.json + config.json + .env + 环境变量。"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS_FILE = PROJECT_ROOT / "config.defaults.json"


def _load_defaults(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取默认配置：{path}（{type(exc).__name__}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"默认配置必须是 JSON 对象：{path}")
    return data


# 保留 DEFAULTS 导入接口，默认值仅在项目根目录 JSON 中维护；重启后重新读取。
DEFAULTS: dict[str, Any] = _load_defaults(DEFAULTS_FILE)


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
    poll = cfg['poll_interval_seconds']
    if type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0:
        raise ValueError('poll_interval_seconds 必须是有限的正数')
    # 旧配置只提供内联提示词时继续使用它；显式提供文件时以文件为准。
    deepseek_data = data.get("deepseek") or {}
    if "system_prompt" in deepseek_data and "system_prompt_file" not in deepseek_data:
        cfg["deepseek"]["system_prompt_file"] = ""
    if cfg["deepseek"].get("reasoning_effort") not in (None, "low", "high", "max"):
        raise ValueError("deepseek.reasoning_effort 必须为 low/high/max")
    window = cfg["target"]["context_window_seconds"]
    if type(window) is not int or window <= 0:
        raise ValueError("target.context_window_seconds 必须是正整数")
    if type(cfg["target"]["trigger"]["pat_enabled"]) is not bool:
        raise ValueError("target.trigger.pat_enabled 必须为布尔值")
    style = cfg['reply_style']
    if not isinstance(style, dict):
        raise ValueError('reply_style 必须为 JSON 对象')
    for key in ('wechat_emoticons_enabled', 'omit_trailing_period'):
        if type(style[key]) is not bool:
            raise ValueError(f'reply_style.{key} 必须为布尔值')
    proactive = cfg['proactive']
    if not isinstance(proactive, dict) or type(proactive['enabled']) is not bool:
        raise ValueError('proactive.enabled 必须为布尔值')
    for key, minimum, maximum in (('window_seconds', 60, 3600), ('min_messages', 2, 100),
            ('check_interval_seconds', 1, 60), ('evaluation_interval_seconds', 30, 3600),
            ('min_new_messages', 1, 100), ('reply_ttl_seconds', 1, 300),
            ('timeout_seconds', 1, 30), ('max_reply_chars', 1, 30),
            ('entry_cooldown_seconds', 60, 3600), ('max_entries_per_hour', 0, 12)):
        if type(proactive[key]) is not int or not minimum <= proactive[key] <= maximum:
            raise ValueError(f'proactive.{key} 超出范围 {minimum}–{maximum}')
    if proactive['reply_ttl_seconds'] < proactive['timeout_seconds']:
        raise ValueError('proactive.reply_ttl_seconds 不能小于 timeout_seconds')
    for section in ('opportunities', 'conversation'):
        if not isinstance(proactive[section], dict) or type(proactive[section].get('enabled')) is not bool:
            raise ValueError(f'proactive.{section}.enabled 必须为布尔值')
    for section, key, minimum, maximum in (
            ('opportunities', 'min_messages', 2, 100), ('opportunities', 'min_senders', 2, 20),
            ('conversation', 'window_seconds', 30, 300), ('conversation', 'max_followups', 0, 2)):
        value = proactive[section].get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'proactive.{section}.{key} 超出范围 {minimum}–{maximum}')
    names = proactive['opportunities'].get('address_names')
    if (not isinstance(names, list) or len(names) > 10
            or any(not isinstance(n, str) or not 1 <= len(n.strip()) <= 32
                   or any(ord(c) < 32 for c in n) for n in names)):
        raise ValueError('proactive.opportunities.address_names 必须为最多10个非空短称呼')

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
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 2:
        raise ValueError("queue.generation_concurrency 必须为 1 或 2")
    memory = cfg["memory"]
    directory = Path(memory["directory"]).expanduser()
    cfg["memory"]["directory"] = str(directory if directory.is_absolute() else PROJECT_ROOT/directory)
    if not isinstance(memory["enabled"], bool) or not isinstance(memory["model"], str) or not memory["model"].strip():
        raise ValueError("memory 开关或模型无效")
    if memory["thinking"] not in ("enabled", "disabled"):
        raise ValueError("memory.thinking 必须为 enabled 或 disabled")
    if memory["reasoning_effort"] not in ("low", "high", "max"):
        raise ValueError("memory.reasoning_effort 必须为 low、high 或 max")
    if not isinstance(memory['system_prompt_file'], str) or not memory['system_prompt_file'].strip():
        raise ValueError('memory.system_prompt_file 必须为提示词文件路径')
    memory_prompt = Path(memory['system_prompt_file']).expanduser()
    memory['system_prompt_file'] = str(memory_prompt if memory_prompt.is_absolute() else PROJECT_ROOT/memory_prompt)
    learning = memory['learning']
    if not isinstance(learning, dict) or any(type(learning.get(key)) is not bool for key in ('enabled', 'passive_enabled')):
        raise ValueError('memory.learning 开关必须为布尔值')
    for key, minimum, maximum in (('window_seconds', 60, 3600), ('min_new_messages', 2, 100),
                                  ('min_senders', 2, 20), ('min_interval_seconds', 300, 86400)):
        if type(learning.get(key)) is not int or not minimum <= learning[key] <= maximum:
            raise ValueError('memory.learning.'+key+' 超出范围')
    for key, minimum, maximum in (("min_interval_seconds", 60, 86400), ("prompt_max_chars", 100, 1200),
                                  ("max_tokens", 256, 8192), ("timeout_seconds", 1, 120)):
        if type(memory[key]) is not int or not minimum <= memory[key] <= maximum:
            raise ValueError(f"memory.{key} 超出范围 {minimum}–{maximum}")
    avatar = memory["avatar"]
    if not isinstance(avatar["enabled"], bool):
        raise ValueError("memory.avatar.enabled 必须为布尔值")
    for key, minimum, maximum in (("max_per_batch", 1, 2), ("max_bytes", 1024, 524288),
                                  ("retry_interval_seconds", 60, 86400)):
        if type(avatar[key]) is not int or not minimum <= avatar[key] <= maximum:
            raise ValueError(f"memory.avatar.{key} 超出范围 {minimum}–{maximum}")
    vision = cfg["vision"]
    if not isinstance(vision["enabled"], bool) or vision["detail"] not in ("original", "high", "low", "auto"):
        raise ValueError("vision.enabled 必须为布尔值，detail 必须为 original/high/low/auto")
    for key, maximum in (("max_images", 4), ("max_image_bytes", 8 * 1024 * 1024)):
        value = vision[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"vision.{key} 必须为 1 到 {maximum} 的整数")
    if not 0 <= float(vision["cache_wait_seconds"]) <= 10:
        raise ValueError("vision.cache_wait_seconds 必须为 0 到 10 秒")
    search = cfg["search"]
    if not isinstance(search["enabled"], bool) or not isinstance(search["feedback_enabled"], bool):
        raise ValueError("search 开关必须为布尔值")
    for key, maximum in (("max_tool_rounds", 3), ("max_requests", 5), ("max_results", 10)):
        value = search[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"search.{key} 必须为 1 到 {maximum} 的整数")
    if not 1 <= float(search["timeout_seconds"]) <= 60 or not 15 <= float(search["total_timeout_seconds"]) <= 300:
        raise ValueError("search 单请求超时须为 1–60 秒，总时限须为 15–300 秒")
    phrases = search["feedback_phrases"]
    if not isinstance(phrases, list) or not phrases or any(not isinstance(p, str) or not 1 <= len(p.strip()) <= 80 for p in phrases):
        raise ValueError("search.feedback_phrases 须为非空短句列表，每句 1–80 字")
    queue = cfg['queue']
    for key, low, high in (('generation_retries', 0, 3), ('completed_retention', 0, 10000)):
        if type(queue[key]) is not int or not low <= queue[key] <= high:
            raise ValueError('queue.'+key+' 超出范围')
    delay = queue['generation_retry_delay_seconds']
    if type(delay) not in (int, float) or not 1 <= delay <= 60:
        raise ValueError('queue.generation_retry_delay_seconds 必须为 1–60 秒')
    from wxbridge.ai.roles import validate_models
    validate_models(cfg)
    return cfg


def deepseek_api_key(cfg: dict[str, Any]) -> str:
    env_name = cfg["deepseek"].get("api_key_env") or "DEEPSEEK_API_KEY"
    return os.environ.get(env_name, "").strip()
