"""Explicit role models and per-group connection selection."""
from copy import deepcopy
import math
from provider import load_config
from provider.adapters import get_protocol_adapter
ROLES = ('reply', 'participation', 'memory', 'avatar')
OPTIONS = {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort', 'thinking_mode', 'thinking_budget_tokens'}

def model_settings(cfg, role='reply', *, provider_config=None):
    if role not in ROLES:
        raise ValueError('unknown_model_role')
    lane = 'reply' if role == 'reply' else 'background'
    config = provider_config if provider_config is not None else load_config(cfg.get('provider_env'))
    connection = cfg['model_clients'][lane]
    settings = deepcopy(cfg['models'][role])
    return dict(settings.get('options', {}), model=settings.get('model', ''), provider=config.protocols[connection], base_url=config.connections[connection].base_url, connection=connection)

def validate_models(cfg):
    if set(cfg['model_clients']) != {'reply', 'image', 'background'}:
        raise ValueError('model_clients requires reply/image/background')
    config = load_config(cfg.get('provider_env'))
    for value in cfg['model_clients'].values():
        if value not in config.connections:
            raise ValueError('unknown_provider_connection')
    if set(cfg['models']) != set(ROLES):
        raise ValueError('invalid_model_roles')
    for role in ROLES:
        value = cfg['models'][role]
        if not isinstance(value, dict) or set(value) - {'model', 'options'}:
            raise ValueError('invalid_model_settings:' + role)
        enabled = role == 'reply' or (role == 'participation' and cfg['proactive']['enabled']) or (role in ('memory', 'avatar') and cfg['memory']['enabled'] and (role != 'avatar' or cfg['memory']['avatar']['enabled']))
        if enabled and (not isinstance(value.get('model'), str) or not value['model'].strip()):
            raise ValueError('missing_model:' + role)
        options = value.get('options', {})
        if not isinstance(options, dict) or set(options) - OPTIONS:
            raise ValueError('invalid_model_options:' + role)
        for key, lo, hi in (('max_tokens', 1, 128000), ('timeout_seconds', 1, 300), ('temperature', 0, 2)):
            v = options.get(key)
            if key == 'temperature' and v is None:
                continue
            if type(v) not in (int, float) or not math.isfinite(v) or (not lo <= v <= hi):
                raise ValueError('invalid_model_option:' + role + '.' + key)
        if type(options['max_tokens']) is not int:
            raise ValueError('invalid_max_tokens')
    if cfg['search'].get('backend') not in ('external', 'native', 'auto'):
        raise ValueError('invalid_search_backend')
    if cfg['search'].get('connection', 'deepseek') not in config.connections:
        raise ValueError('invalid_search_connection')
    if cfg['search']['enabled'] and cfg['search']['backend'] == 'native' and (not get_protocol_adapter(model_settings(cfg)['provider']).native_search_supported):
        raise ValueError('unsupported_native_search')

def explain_config(cfg):
    return dict(instance=cfg.get('_instance_id'), models={role: dict(settings=model_settings(cfg, role)) for role in ROLES}, clients=deepcopy(cfg['model_clients']))
