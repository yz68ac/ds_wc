"""Explicit role models and per-group connection selection."""
from copy import deepcopy
import math
from provider import load_config
from provider.adapters import get_protocol_adapter
ROLES = ('reply', 'participation', 'memory', 'avatar', 'group_profile')
OPTIONS = {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort', 'thinking_mode', 'thinking_budget_tokens'}

def model_settings(cfg, role='reply', *, provider_config=None):
    if role not in (*ROLES, 'image'):
        raise ValueError('unknown_model_role')
    lane = role if role in ('reply', 'image') else 'background'
    config = provider_config if provider_config is not None else load_config(cfg.get('provider_env'))
    connection = cfg['model_clients'][lane]
    settings = deepcopy(cfg['models'][role])
    provider, base_url = config.protocols[connection], config.connections[connection].base_url
    if role == 'image' and config.options[connection].get('image_protocol') == 'gemini':
        provider, base_url = 'gemini', config.options[connection].get('image_base_url', '')
    return dict(settings.get('options', {}), model=settings.get('model', ''), provider=provider, base_url=base_url, connection=connection)

def validate_models(cfg):
    if set(cfg['model_clients']) != {'reply', 'image', 'background'}:
        raise ValueError('model_clients requires reply/image/background')
    config = load_config(cfg.get('provider_env'))
    for value in cfg['model_clients'].values():
        if value not in config.connections:
            raise ValueError('unknown_provider_connection')
    if set(cfg['models']) != {*ROLES, 'image'}:
        raise ValueError('invalid_model_roles')
    image = cfg.get('image_generation')
    if not isinstance(image, dict) or set(image) != {'enabled'} or type(image['enabled']) is not bool:
        raise ValueError('invalid_image_generation_settings')
    validate_image_model(cfg, provider_config=config)
    private = cfg['target']['chat'].startswith('wxid_') and not cfg['target']['chat'].endswith('@chatroom')
    for role in ROLES:
        value = cfg['models'][role]
        if not isinstance(value, dict) or set(value) - {'model', 'options'}:
            raise ValueError('invalid_model_settings:' + role)
        enabled = (role == 'reply' or (role == 'participation' and cfg['participation']['enabled'] and not private)
                   or (role == 'group_profile' and cfg['profile']['enabled'])
                   or (role in ('memory', 'avatar') and cfg['memory']['enabled']
                       and (role != 'avatar' or cfg['memory']['avatar']['enabled'])))
        if enabled and (not isinstance(value.get('model'), str) or not value['model'].strip()):
            raise ValueError('missing_model:' + role)
        options = value.get('options', {})
        if not isinstance(options, dict) or set(options) - OPTIONS:
            raise ValueError('invalid_model_options:' + role)
        connection = cfg['model_clients']['reply' if role == 'reply' else 'background']
        optional_tokens = ('max_tokens' in options and options['max_tokens'] is None
                           and config.protocols[connection] in ('deepseek', 'openai', 'openai_compatible'))
        for key, lo, hi in (('max_tokens', 1, 128000), ('timeout_seconds', 1, 300), ('temperature', 0, 2)):
            v = options.get(key)
            if key == 'max_tokens' and optional_tokens:
                continue
            if key == 'temperature' and v is None:
                continue
            if type(v) not in (int, float) or not math.isfinite(v) or (not lo <= v <= hi):
                raise ValueError('invalid_model_option:' + role + '.' + key)
        if not optional_tokens and type(options['max_tokens']) is not int:
            raise ValueError('invalid_max_tokens')
    if cfg['search'].get('backend') not in ('external', 'native', 'auto'):
        raise ValueError('invalid_search_backend')
    if cfg['search'].get('connection', 'deepseek') not in config.connections:
        raise ValueError('invalid_search_connection')
    if cfg['search']['enabled'] and cfg['search']['backend'] == 'native' and (not get_protocol_adapter(model_settings(cfg)['provider']).native_search_supported):
        raise ValueError('unsupported_native_search')

def explain_config(cfg):
    return dict(instance=cfg.get('_instance_id'), models={role: dict(settings=model_settings(cfg, role)) for role in (*ROLES, 'image')}, clients=deepcopy(cfg['model_clients']))


def image_request(cfg, prompt):
    from provider import ImageRequest
    settings = cfg['models']['image']
    return ImageRequest(settings['model'], prompt, deepcopy(settings.get('options', {})))


def validate_image_model(cfg, *, provider_config):
    from provider import ModelError
    from provider.images import IMAGE_PROTOCOLS, build_image_payload
    from provider.gemini_images import build_gemini_image_payload
    value = cfg['models']['image']
    if (not isinstance(value, dict) or set(value) - {'model', 'options'}
            or not isinstance(value.get('model'), str) or not isinstance(value.get('options', {}), dict)):
        raise ValueError('invalid_model_settings:image')
    connection = cfg['model_clients']['image']
    gemini = provider_config.options[connection].get('image_protocol') == 'gemini'
    enabled = cfg['image_generation']['enabled']
    if enabled and not value['model'].strip():
        raise ValueError('missing_model:image')
    if enabled and not gemini and provider_config.protocols[connection] not in IMAGE_PROTOCOLS:
        raise ValueError('unsupported_image_generation')
    if enabled and gemini and not provider_config.options[connection].get('image_base_url'):
        raise ValueError('missing_image_base_url')
    if enabled and cfg['ui']['backend'] != 'native_service' and not cfg['ui']['dry_run']:
        raise ValueError('image_generation_requires_native_service')
    # Validate options even for a disabled task; a blank model is valid until enabled.
    from dataclasses import replace
    request = replace(image_request(cfg, 'validation'), model=value['model'] or 'validation')
    if request.options.get('n', 1) != 1:
        raise ValueError('image_generation_requires_one_image')
    try:
        (build_gemini_image_payload if gemini else build_image_payload)(request)
    except ModelError as exc:
        raise ValueError(exc.code) from None
