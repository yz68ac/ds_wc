"""Account config and independently validated instance/<id>/config.json files."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from wxbridge.config_merge import deep_merge as _deep_merge, field_origins

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    p = Path(path)
    from wxbridge.instances import discover_instances, validate_account_fields
    data = json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
    validate_account_fields(data)
    cfg = resolve_config(data, p, validate_role_models=False)
    cfg['system_prompt_file'] = str((PROJECT_ROOT/DEFAULTS['system_prompt_file']).resolve())
    cfg['instances'] = discover_instances(cfg, p, resolver=resolve_config)
    return cfg


def resolve_config(data: dict, path: str | Path, *, environment=True, origins=None, validate_role_models=True) -> dict[str, Any]:
    """Resolve one effective configuration without loading credentials again."""
    if not isinstance(data, dict):
        raise ValueError('配置必须为 JSON 对象')
    p = Path(path)
    # Derived values cannot be supplied by JSON or copied into another instance.
    data = {k: v for k, v in data.items() if k not in ('_compiled_models', '_config_origins')}
    cfg = _deep_merge(DEFAULTS, data)
    provenance = dict(origins) if origins is not None else {
        **field_origins(DEFAULTS, 'defaults'), **field_origins(data, 'config')}
    from wxbridge.wechat.transport import backend
    backend(cfg)
    poll = cfg['poll_interval_seconds']
    if type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0:
        raise ValueError('poll_interval_seconds 必须是有限的正数')
    obsolete = {'deepseek', 'model_profile', 'model_profiles', 'persona'} & set(data)
    if 'generation_concurrency' in data.get('queue', {}):
        obsolete.add('queue.generation_concurrency')
    if obsolete:
        raise ValueError('obsolete_model_configuration:'+','.join(sorted(obsolete)))
    window = cfg["target"]["context_window_seconds"]
    if cfg['target'].get('context_budget_strategy', 'strict') not in ('strict', 'recent'):
        raise ValueError('target.context_budget_strategy must be strict/recent')
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
    if environment and os.environ.get("WX_DB_DIR"):
        cfg["db_dir"] = os.environ["WX_DB_DIR"]

    # 统一转成绝对路径（相对于配置文件所在目录）
    base = p.resolve().parent
    for key in ("keys_file", "decrypted_dir", "state_file", "log_file"):
        value = str(cfg.get(key) or "")
        if value and not os.path.isabs(value):
            cfg[key] = str((base / value).resolve())
    if cfg.get("provider_env"):
        provider_path = Path(cfg["provider_env"]).expanduser()
        cfg["provider_env"] = str(provider_path if provider_path.is_absolute() else (base/provider_path).resolve())
    prompt_file = cfg.get("system_prompt_file")
    if prompt_file:
        prompt_path = Path(prompt_file).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = base / prompt_path
        cfg["system_prompt_file"] = str(prompt_path.resolve())
    queue_path = Path(cfg["queue"]["file"]).expanduser()
    if not queue_path.is_absolute():
        queue_path = base / queue_path
    cfg["queue"]["file"] = str(queue_path.resolve())
    if (type(cfg['queue']['debounce_seconds']) not in (int, float)
            or not math.isfinite(float(cfg['queue']['debounce_seconds'])) or float(cfg["queue"]["debounce_seconds"]) < 0):
        raise ValueError("queue.debounce_seconds 必须大于或等于 0（0 表示每次 @ 独立排队）")
    if (type(cfg['ui']['send_confirm_timeout_seconds']) not in (int, float)
            or not math.isfinite(float(cfg['ui']['send_confirm_timeout_seconds'])) or float(cfg["ui"]["send_confirm_timeout_seconds"]) <= 0):
        raise ValueError("ui.send_confirm_timeout_seconds 必须大于 0")
    memory = cfg["memory"]
    directory = Path(memory["directory"]).expanduser()
    cfg["memory"]["directory"] = str(directory if directory.is_absolute() else base/directory)
    if type(memory['enabled']) is not bool:
        raise ValueError('memory.enabled must be boolean')
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
    for key, minimum, maximum in (("min_interval_seconds", 60, 86400), ("prompt_max_chars", 100, 1200)):
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
    if type(vision.get('download_enabled')) is not bool:
        raise ValueError('vision.download_enabled 必须为布尔值')
    if vision.get('download_backend', 'desktop') not in ('desktop', 'native'):
        raise ValueError('vision.download_backend 必须为 desktop 或 native')
    download_timeout = vision.get('download_timeout_seconds')
    if (type(download_timeout) not in (int, float) or not math.isfinite(download_timeout)
            or not 5 <= download_timeout <= 30):
        raise ValueError('vision.download_timeout_seconds 必须为5到30秒')
    if vision.get('download_backend') == 'native' and type(download_timeout) is not int:
        raise ValueError('原生图片下载超时必须为5到30秒的整数')
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
    for key, default, maximum in (('max_pending', 32, 1000), ('max_pending_per_sender', 8, 1000),
                                  ('generation_lookahead', 4, 32), ('intake_max_pages', 4, 20)):
        value = queue.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('queue.'+key+' exceeds its allowed range')
        queue[key] = value
    if queue['max_pending_per_sender'] > queue['max_pending']:
        raise ValueError('queue.max_pending_per_sender exceeds max_pending')
    for key, low, high in (('generation_retries', 0, 3), ('completed_retention', 0, 10000)):
        if type(queue[key]) is not int or not low <= queue[key] <= high:
            raise ValueError('queue.'+key+' 超出范围')
    delay = queue['generation_retry_delay_seconds']
    if type(delay) not in (int, float) or not 1 <= delay <= 60:
        raise ValueError('queue.generation_retry_delay_seconds 必须为 1–60 秒')
    from wxbridge.ai.roles import validate_models
    if validate_role_models:
        validate_models(cfg)
    cfg['_config_origins'] = provenance
    return cfg


def explain_config(cfg: dict[str, Any]) -> dict:
    """Safe effective model settings for CLI diagnostics; never reads keys."""
    from wxbridge.ai.roles import explain_config as explain
    return explain(cfg)
