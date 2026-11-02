"""Document-boundary validation preserving existing configuration contracts."""
from __future__ import annotations
from copy import deepcopy
import math, os
from pathlib import Path
from typing import Any
from wxbridge.config_merge import deep_merge as _deep_merge, field_origins
from wxbridge.paths import resource_path
from .codec import default_document
from .models import MemoryConfig
ROLES = ('reply', 'participation', 'memory', 'avatar', 'group_profile')
OPTIONS = {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort', 'thinking_mode', 'thinking_budget_tokens'}


def provider_load(path):
    from provider import load_config
    return load_config(path)

def validate_document(data: dict, path, *, environment=True, origins=None, validate_role_models=True):
    """Only the file boundary handles mutable JSON; classes own field constraints."""
    from .codec import account_from_document, instance_from_document
    if not isinstance(data,dict):
        raise ValueError('配置必须为 JSON 对象')
    p=Path(path)
    for section in ('queue','target'):
        if not isinstance(data.get(section,{}),dict):
            raise ValueError(section+' 必须为 JSON 对象')
    obsolete={'deepseek','model_profile','model_profiles','persona','proactive'} & set(data)
    for section,keys in (('queue',('generation_concurrency','debounce_seconds')),('target',('trigger','context_window_seconds'))):
        for key in keys:
            if key in data.get(section,{}):
                obsolete.add(section+'.'+key+('; use migrate_context_data' if key=='context_window_seconds' else ''))
    if obsolete:
        raise ValueError('obsolete_model_configuration:'+','.join(sorted(obsolete)))
    cfg=_deep_merge(default_document(),data)
    provenance=dict(origins) if origins is not None else {**field_origins(default_document(),'defaults'),**field_origins(data,'config')}
    if environment and os.environ.get('WX_DB_DIR'):
        cfg['db_dir']=os.environ['WX_DB_DIR']
    base=p.resolve().parent
    for key in ('keys_file','decrypted_dir','state_file','log_file'):
        value=str(cfg.get(key) or '')
        if value and not os.path.isabs(value):cfg[key]=str((base/value).resolve())
    if cfg.get('provider_env'):
        value=Path(cfg['provider_env']).expanduser()
        cfg['provider_env']=str(value if value.is_absolute() else base/value)
    if cfg.get('system_prompt_file'):
        value=Path(cfg['system_prompt_file']).expanduser()
        cfg['system_prompt_file']=str((value if value.is_absolute() else base/value).resolve())
    value=Path(cfg['queue']['file']).expanduser()
    cfg['queue']['file']=str(value if value.is_absolute() else (base/value).resolve())
    directory=Path(cfg['memory']['directory']).expanduser()
    cfg['memory']['directory']=str(directory if directory.is_absolute() else base/directory)
    prompt=cfg['memory']['system_prompt_file']
    if not isinstance(prompt,str) or not prompt.strip():
        raise ValueError('memory.system_prompt_file 必须为提示词文件路径')
    prompt=Path(prompt).expanduser()
    if not prompt.is_absolute():
        prompt=resource_path(prompt) if str(prompt).replace('\\','/')==MemoryConfig().system_prompt_file else base/prompt
    cfg['memory']['system_prompt_file']=str(prompt)
    account=account_from_document(cfg).validate()
    instance=instance_from_document(cfg,account=account).validate()
    if instance.profile.min_new_messages>instance.profile.batch_size:
        raise ValueError('profile.min_new_messages must not exceed batch_size')
    if instance.participation.max_wait_seconds<instance.participation.debounce_seconds:
        raise ValueError('participation.max_wait_seconds cannot be smaller than debounce_seconds')
    if instance.queue.max_pending_per_sender>instance.queue.max_pending:
        raise ValueError('queue.max_pending_per_sender exceeds max_pending')
    if instance.vision.download_backend=='native' and type(instance.vision.download_timeout_seconds) is not int:
        raise ValueError('原生图片下载超时必须为5到30秒的整数')
    if validate_role_models:validate_models(cfg)
    cfg['_config_origins']=provenance
    return cfg



def validate_models(cfg):
    from provider.adapters import get_protocol_adapter
    if set(cfg['model_clients']) != {'reply', 'image', 'background'}:
        raise ValueError('model_clients requires reply/image/background')
    config = provider_load(cfg.get('provider_env'))
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
                           and config.protocols[connection] in ('deepseek', 'openai', 'openai_compatible', 'gemini'))
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
    if cfg['search']['enabled'] and cfg['search']['backend'] == 'native' and (not get_protocol_adapter(config.protocols[cfg['model_clients']['reply']]).native_search_supported):
        raise ValueError('unsupported_native_search')


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
    from provider import ImageRequest
    request = ImageRequest(value['model'] or 'validation', 'validation', deepcopy(value.get('options', {})))
    if request.options.get('n', 1) != 1:
        raise ValueError('image_generation_requires_one_image')
    try:
        (build_gemini_image_payload if gemini else build_image_payload)(request)
    except ModelError as exc:
        raise ValueError(exc.code) from None
