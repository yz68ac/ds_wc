"""Account config and independently validated instance/<id>/config.json files."""
from __future__ import annotations

from copy import deepcopy
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


def migrate_instance_data(data: dict[str, Any]) -> dict[str, Any]:
    """Explicit one-time JSON conversion; normal loading never calls this helper.

    Return an independent document and preserve identity, enable state, paths,
    models and unknown business fields. The caller chooses where to save it.
    """
    if not isinstance(data, dict):
        raise ValueError('instance_config_must_be_object')
    result = deepcopy(data)
    old = result.pop('proactive', None)
    target, queue = result.get('target', {}), result.get('queue', {})
    if not isinstance(target, dict) or not isinstance(queue, dict) or (old is not None and not isinstance(old, dict)):
        raise ValueError('invalid_legacy_participation_configuration')
    trigger = target.pop('trigger', None)
    if trigger is not None and not isinstance(trigger, dict):
        raise ValueError('invalid_legacy_participation_configuration')
    debounce = queue.pop('debounce_seconds', None)
    if old is None and trigger is None and debounce is None:
        return result
    existing = result.get('participation', {})
    if not isinstance(existing, dict):
        raise ValueError('invalid_participation_settings')
    migrated = deepcopy(DEFAULTS['participation'])
    if old is not None:
        migrated['enabled'] = old.get('enabled', False)
        opportunities = old.get('opportunities', {})
        if not isinstance(opportunities, dict):
            raise ValueError('invalid_legacy_participation_configuration')
        names = opportunities.get('address_names', [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('invalid_legacy_participation_configuration')
        migrated['address_names'] = list(names)
        if 'window_seconds' in old:
            target.setdefault('context_window_seconds', old['window_seconds'])
            result['target'] = target
    if trigger is not None:
        migrated['pat_enabled'] = trigger.get('pat_enabled', True)
        names = trigger.get('mentions', [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('invalid_legacy_participation_configuration')
        migrated['address_names'] += [n.lstrip('@') for n in names if n.lstrip('@').strip()]
        migrated['address_names'] = list(dict.fromkeys(migrated['address_names']))
    if debounce is not None:
        migrated['debounce_seconds'] = debounce
        if type(debounce) in (int, float) and math.isfinite(debounce):
            migrated['max_wait_seconds'] = max(migrated['max_wait_seconds'], debounce)
    migrated.update(existing)
    result['participation'] = migrated
    return result


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
    for section in ('queue', 'target'):
        if not isinstance(data.get(section, {}), dict):
            raise ValueError(section+' 必须为 JSON 对象')
    poll = cfg['poll_interval_seconds']
    if type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0:
        raise ValueError('poll_interval_seconds 必须是有限的正数')
    obsolete = {'deepseek', 'model_profile', 'model_profiles', 'persona', 'proactive'} & set(data)
    if 'generation_concurrency' in data.get('queue', {}):
        obsolete.add('queue.generation_concurrency')
    if 'debounce_seconds' in data.get('queue', {}):
        obsolete.add('queue.debounce_seconds')
    if 'trigger' in data.get('target', {}):
        obsolete.add('target.trigger')
    if obsolete:
        raise ValueError('obsolete_model_configuration:'+','.join(sorted(obsolete)))
    profile = cfg['profile']
    profile_fields = {'enabled', 'initial_history_days', 'initial_max_messages', 'batch_size',
                      'min_new_messages', 'min_interval_seconds', 'summary_max_chars', 'analysis_max_chars'}
    if (not isinstance(profile, dict) or set(profile) != profile_fields
            or type(profile.get('enabled')) is not bool):
        raise ValueError('invalid_profile_settings')
    for key, minimum, maximum in (
            ('initial_history_days', 1, 30), ('initial_max_messages', 1, 1000),
            ('batch_size', 1, 100), ('min_new_messages', 1, 1000),
            ('min_interval_seconds', 60, 86400), ('summary_max_chars', 100, 4000),
            ('analysis_max_chars', 2000, 100000)):
        if type(profile.get(key)) is not int or not minimum <= profile[key] <= maximum:
            raise ValueError('invalid_profile_option:' + key)
    if profile['min_new_messages'] > profile['batch_size']:
        raise ValueError('profile.min_new_messages must not exceed batch_size')
    window = cfg["target"]["context_window_seconds"]
    if cfg['target'].get('context_budget_strategy', 'strict') not in ('strict', 'recent'):
        raise ValueError('target.context_budget_strategy must be strict/recent')
    if type(window) is not int or window <= 0:
        raise ValueError("target.context_window_seconds 必须是正整数")
    age = cfg['target']['max_message_age_seconds']
    if type(age) not in (int, float) or not math.isfinite(age) or not 1 <= age <= 3600:
        raise ValueError('target.max_message_age_seconds 必须为 1–3600 秒')
    style = cfg['reply_style']
    if not isinstance(style, dict):
        raise ValueError('reply_style 必须为 JSON 对象')
    for key in ('wechat_emoticons_enabled', 'omit_trailing_period'):
        if type(style[key]) is not bool:
            raise ValueError(f'reply_style.{key} 必须为布尔值')
    participation = cfg['participation']
    fields = {'enabled', 'debounce_seconds', 'max_wait_seconds', 'max_batch_messages',
              'address_names', 'pat_enabled'}
    if not isinstance(participation, dict) or set(participation) != fields:
        raise ValueError('invalid_participation_settings')
    for key in ('enabled', 'pat_enabled'):
        if type(participation[key]) is not bool:
            raise ValueError('participation.'+key+' 必须为布尔值')
    for key, minimum in (('debounce_seconds', 0), ('max_wait_seconds', 0.1)):
        value = participation[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 60:
            raise ValueError('participation.'+key+' 超出允许范围')
    if participation['max_wait_seconds'] < participation['debounce_seconds']:
        raise ValueError('participation.max_wait_seconds 不能小于 debounce_seconds')
    size = participation['max_batch_messages']
    if type(size) is not int or not 1 <= size <= 32:
        raise ValueError('participation.max_batch_messages 必须为 1–32 的整数')
    names = participation['address_names']
    if (not isinstance(names, list) or len(names) > 10
            or any(not isinstance(n, str) or not 1 <= len(n.strip()) <= 32
                   or any(ord(c) < 32 for c in n) for n in names)):
        raise ValueError('participation.address_names 必须为最多10个非空短称呼')

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
