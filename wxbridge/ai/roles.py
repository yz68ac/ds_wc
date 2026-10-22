"""模型角色解析：保留旧配置，角色覆盖值只影响对应请求。"""
from __future__ import annotations

import math
from urllib.parse import urlsplit

ROLES = ('reply', 'participation', 'memory', 'avatar')
FIELDS = {'provider', 'base_url', 'api_key_env', 'model', 'thinking', 'reasoning_effort',
          'temperature', 'timeout_seconds', 'max_tokens'}


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
        result.update(thinking='disabled', reasoning_effort=None, max_tokens=600)
    result.update(cfg.get('models', {}).get(role, {}))
    return result


def validate_models(cfg):
    overrides = cfg.get('models', {})
    if not isinstance(overrides, dict) or set(overrides)-set(ROLES):
        raise ValueError('models 只能包含 reply/participation/memory/avatar')
    for role in ROLES:
        override = overrides.get(role, {})
        if not isinstance(override, dict) or set(override)-FIELDS:
            raise ValueError('models.'+role+' 包含无效字段')
        settings = model_settings(cfg, role)
        if settings.get('provider') not in ('deepseek', 'openai'):
            raise ValueError('models.'+role+'.provider 必须为 deepseek 或 openai')
        for key in ('base_url', 'api_key_env', 'model'):
            if not isinstance(settings.get(key), str) or not settings[key].strip():
                raise ValueError('models.'+role+'.'+key+' 不能为空')
        url = urlsplit(settings['base_url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('models.'+role+'.base_url 无效')
        if settings.get('thinking') not in ('enabled', 'disabled'):
            raise ValueError('models.'+role+'.thinking 无效')
        if settings.get('reasoning_effort') not in (None, 'low', 'high', 'max'):
            raise ValueError('models.'+role+'.reasoning_effort 无效')
        for key, low, high in (('timeout_seconds', 1, 300), ('max_tokens', 1, 32768), ('temperature', 0, 2)):
            value = settings.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('models.'+role+'.'+key+' 超出范围')
        if type(settings['max_tokens']) is not int:
            raise ValueError('models.'+role+'.max_tokens 必须为整数')
    if model_settings(cfg, 'participation')['timeout_seconds'] > cfg['proactive']['reply_ttl_seconds']:
        raise ValueError('参与判断超时不能超过 proactive.reply_ttl_seconds')
