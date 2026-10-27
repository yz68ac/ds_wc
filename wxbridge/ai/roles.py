"""模型角色解析：保留旧配置，角色覆盖值只影响对应请求。"""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit
from wxbridge.ai.providers import get_adapter, supported_providers

ROLES = ('reply', 'participation', 'memory', 'avatar')
FIELDS = {'provider', 'base_url', 'api_key_env', 'model', 'thinking', 'reasoning_effort',
          'temperature', 'timeout_seconds', 'max_tokens', 'profile', 'thinking_mode',
          'thinking_budget_tokens', 'anthropic_version', 'anthropic_auth', 'anthropic_web_search_version',
          'native_search_supported'}
PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')


def model_settings(cfg, role='reply'):
    if role not in ROLES:
        raise ValueError('unknown_model_role')
    result = dict(cfg.get('deepseek', {}))
    result.setdefault('provider', 'deepseek')
    if role in ('memory', 'avatar'):
        result.update({k: v for k, v in cfg.get('memory', {}).items()
                       if k in ('model', 'thinking', 'reasoning_effort', 'max_tokens', 'timeout_seconds')})
    if role == 'participation':
        result.update(thinking='disabled', reasoning_effort=None, max_tokens=256,
                      timeout_seconds=cfg.get('proactive', {}).get('timeout_seconds', 20))
    if role == 'avatar':
        result.update(thinking='enabled', reasoning_effort='max', max_tokens=1200)
    override = cfg.get('models', {}).get(role, {})
    profile = override.get('profile')
    if profile is None:profile = cfg.get('model_profile')
    if profile is not None:
        profiles = cfg.get('model_profiles', {})
        if not isinstance(profile, str) or not isinstance(profiles, dict) or profile not in profiles:
            raise ValueError('model_profile 不存在')
        result.update(profiles[profile])
    result.update({k: v for k, v in override.items() if k != 'profile'})
    return result


def validate_models(cfg):
    profiles = cfg.get('model_profiles', {})
    if not isinstance(profiles, dict) or len(profiles) > 16:
        raise ValueError('model_profiles 必须是最多16项的配置对象')
    for name, profile in profiles.items():
        if (not isinstance(name, str) or not PROFILE_NAME.fullmatch(name) or not isinstance(profile, dict)
                or set(profile)-(FIELDS-{'profile'}) or not {'provider', 'base_url', 'api_key_env', 'model'} <= set(profile)):
            raise ValueError('model_profiles 包含无效连接配置')
        settings = dict(cfg.get('deepseek', {}));settings.update(profile)
        _validate_settings(settings, 'model_profiles')
    selection = cfg.get('model_profile')
    if selection is not None and (not isinstance(selection, str) or selection not in profiles):
        raise ValueError('model_profile 不存在')
    overrides = cfg.get('models', {})
    if not isinstance(overrides, dict) or set(overrides)-set(ROLES):
        raise ValueError('models 只能包含 reply/participation/memory/avatar')
    for role in ROLES:
        override = overrides.get(role, {})
        if not isinstance(override, dict) or set(override)-FIELDS:
            raise ValueError('models.'+role+' 包含无效字段')
        if override.get('profile') is not None and (not isinstance(override['profile'], str) or override['profile'] not in profiles):
            raise ValueError('models.'+role+'.profile 不存在')
        _validate_settings(model_settings(cfg, role), 'models.'+role)
    if model_settings(cfg, 'participation')['timeout_seconds'] > cfg['proactive']['reply_ttl_seconds']:
        raise ValueError('参与判断超时不能超过 proactive.reply_ttl_seconds')
    search = cfg.get('search', {})
    if search.get('backend', 'external') not in ('external', 'native', 'auto'):
        raise ValueError('search.backend 必须为 external/native/auto')
    if 'api_key_env' in search and (not isinstance(search['api_key_env'], str)
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', search['api_key_env'])):
        raise ValueError('search.api_key_env 无效')
    if search.get('enabled', True) and search.get('backend', 'external') == 'native':
        settings = model_settings(cfg, 'reply')
        if not get_adapter(settings['provider']).native_search_supported or settings.get('native_search_supported') is False:
            raise ValueError('当前 reply 协议不支持原生联网，请使用 external')


def _validate_settings(settings, path):
    provider = settings.get('provider', 'deepseek')
    if provider not in supported_providers():raise ValueError(path+'.provider 无效')
    for key in ('base_url', 'api_key_env', 'model'):
        if not isinstance(settings.get(key), str) or not settings[key].strip():raise ValueError(path+'.'+key+' 不能为空')
    url = urlsplit(settings['base_url'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError(path+'.base_url 无效')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', settings['api_key_env']):raise ValueError(path+'.api_key_env 无效')
    if settings.get('thinking') not in ('enabled', 'disabled'):raise ValueError(path+'.thinking 无效')
    efforts = (None, 'none', 'low', 'medium', 'high', 'xhigh', 'max')
    if provider == 'deepseek':efforts = (None, 'low', 'high', 'max')
    if settings.get('reasoning_effort') not in efforts:raise ValueError(path+'.reasoning_effort 无效')
    if settings.get('thinking') == 'enabled' and settings.get('reasoning_effort') == 'none':
        raise ValueError(path+'.thinking 与 reasoning_effort 冲突')
    for key, low, high in (('timeout_seconds', 1, 300), ('max_tokens', 1, 128000), ('temperature', 0, 2)):
        value = settings.get(key)
        if key == 'temperature' and value is None:continue
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(path+'.'+key+' 超出范围')
    if type(settings['max_tokens']) is not int:raise ValueError(path+'.max_tokens 必须为整数')
    if 'native_search_supported' in settings and type(settings['native_search_supported']) is not bool:
        raise ValueError(path+'.native_search_supported 必须为布尔值')
    if provider in ('anthropic', 'anthropic_messages'):
        mode = settings.get('thinking_mode', 'adaptive')
        if mode not in ('adaptive', 'manual', 'between_tools'):raise ValueError(path+'.thinking_mode 无效')
        if settings.get('thinking') == 'enabled' and mode == 'between_tools':raise ValueError(path+'.thinking_mode 与 thinking 冲突')
        if settings.get('thinking') == 'enabled' and mode == 'manual':
            budget = settings.get('thinking_budget_tokens', 1024)
            if type(budget) is not int or not 1024 <= budget < settings['max_tokens']:raise ValueError(path+'.thinking_budget_tokens 无效')
            if settings.get('reasoning_effort') is not None:raise ValueError(path+'.manual 模式请设置 reasoning_effort=null')
        if settings.get('anthropic_auth', 'api_key') not in ('api_key', 'bearer'):raise ValueError(path+'.anthropic_auth 无效')
        if settings.get('anthropic_version', '2023-06-01') not in ('2023-06-01', '2026-10-21'):raise ValueError(path+'.anthropic_version 无效')
        if settings.get('anthropic_web_search_version', 'web_search_20250305') not in ('web_search_20250305', 'web_search_20260209', 'web_search_20260318'):
            raise ValueError(path+'.anthropic_web_search_version 无效')
        temp = settings.get('temperature')
        if temp is not None and temp > 1:raise ValueError(path+'.temperature 超出 Anthropic 范围')
