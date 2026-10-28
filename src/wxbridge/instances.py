"""Discover independent instance directories while retaining shared account resources."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re

from wxbridge.config_merge import deep_merge, field_origins

INSTANCE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z')
CHAT_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}@chatroom\Z')
GROUP_FIELDS = {'system_prompt_file', 'target', 'model_clients', 'models',
                'reply_style', 'proactive', 'memory', 'search', 'vision', 'queue'}


def validate_account_fields(data):
    """Root JSON contains account resources only, never group overrides."""
    allowed = {'db_dir', 'keys_file', 'decrypted_dir', 'state_file', 'log_file',
               'wechat_process', 'source', 'account', 'poll_interval_seconds',
               'ui', 'native_api', 'provider_env', 'queue', 'memory'}
    if not isinstance(data, dict) or set(data)-allowed:
        raise ValueError('root_config_requires_account_fields_only; put group settings in instance/<id>/config.json')
    for section, keys in (('queue', {'file'}), ('memory', {'directory'})):
        if section in data and (not isinstance(data[section], dict) or set(data[section])-keys):
            raise ValueError('root_config.'+section+' only accepts '+','.join(sorted(keys)))


def discover_instances(cfg, path, *, resolver):
    """Load flat group configs beside the account JSON, without network access."""
    import json
    account = cfg.get('account', {})
    if not isinstance(account, dict) or set(account)-{'send_interval_seconds'}:
        raise ValueError('account only accepts send_interval_seconds')
    gap = account.get('send_interval_seconds', 1)
    if type(gap) not in (int, float) or not math.isfinite(gap) or not 0 <= gap <= 60:
        raise ValueError('account.send_interval_seconds must be 0..60')
    cfg['account'] = dict(send_interval_seconds=gap)
    if os.environ.get('WX_GROUP'):
        raise ValueError('WX_GROUP is retired; use instance/<id>/config.json chat')
    directory = Path(path).resolve().parent/'instance'
    if directory.is_symlink():
        raise ValueError('unsafe_instance_directory')
    files = sorted(directory.glob('*/config.json')) if directory.exists() else []
    if len(files) > 16:
        raise ValueError('too_many_instances')
    result, ids, chats = [], set(), set()
    for file in files:
        identifier = file.parent.name
        if file.is_symlink() or file.parent.is_symlink():
            raise ValueError('unsafe_instance_config')
        if not INSTANCE_ID.fullmatch(identifier) or identifier.lower() in ids:
            raise ValueError('invalid_or_duplicate_instance_id:'+identifier)
        ids.add(identifier.lower())
        entry = json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(entry, dict) or set(entry)-GROUP_FIELDS-{'chat', 'enabled'}:
            raise ValueError('invalid_instance_fields:'+identifier)
        enabled, chat = entry.get('enabled', True), entry.get('chat', '')
        if type(enabled) is not bool or not isinstance(chat, str):
            raise ValueError('invalid_instance_identity:'+identifier)
        if (enabled and not CHAT_ID.fullmatch(chat)) or (chat and not CHAT_ID.fullmatch(chat)) or (chat and chat in chats):
            raise ValueError('instance_chat_requires_unique_stable_id:'+identifier)
        if chat:
            chats.add(chat)
        override = {k: copy.deepcopy(v) for k,v in entry.items() if k in GROUP_FIELDS}
        for section in GROUP_FIELDS-{'system_prompt_file'}:
            if section in override and not isinstance(override[section], dict):
                raise ValueError('invalid_instance_section:'+identifier+'.'+section)
        for section, forbidden in (('target', {'chat'}), ('queue', {'file'}), ('memory', {'directory'})):
            if set(override.get(section, {})) & forbidden:
                raise ValueError('instance_cannot_override_account_resource:'+identifier+'.'+section)
        if 'system_prompt_file' in override and (not isinstance(override['system_prompt_file'], str) or not override['system_prompt_file'].strip()):
            raise ValueError('invalid_system_prompt_file:'+identifier)
        if 'system_prompt_file' in override:
            override['system_prompt_file'] = str((file.parent/override['system_prompt_file']).resolve())
        if 'system_prompt_file' in override.get('memory', {}):
            override['memory']['system_prompt_file'] = str((file.parent/override['memory']['system_prompt_file']).resolve())
        effective = deep_merge(cfg, override)
        effective.pop('instances', None)
        effective['target']['chat'] = chat
        runtime = Path(cfg['queue']['file']).parent/'instances'/identifier
        effective['queue']['file'] = str(runtime/'queue.sqlite3')
        effective['state_file'] = str(runtime/'state.json')
        origins = {**cfg.get('_config_origins', {}), **field_origins(override, 'instance.'+identifier)}
        effective = resolver(effective, file, environment=False, origins=origins, validate_role_models=enabled)
        effective['instances'] = []
        effective['_instance_id'] = identifier
        effective['_instance_config_file'] = str(file.resolve())
        result.append(dict(id=identifier, chat=chat, enabled=enabled, reuse_legacy_state=False, config=effective))
    return result


def enabled_instances(cfg):
    """Return fresh effective configs; the caller may enforce runtime policy."""
    return [(e['id'], copy.deepcopy(e['config'])) for e in cfg.get('instances', []) if e['enabled']]


def select_instance(cfg, identifier):
    for entry in cfg.get('instances', []):
        if entry['id'] == identifier:
            return copy.deepcopy(entry['config'])
    raise ValueError('未知实例ID')


def runtime_locks(cfg):
    paths = {str(Path(cfg['decrypted_dir'])/'.listener.lock'), cfg['queue']['file']+'.lock'}
    for _, active in enabled_instances(cfg):
        paths.add(active['queue']['file']+'.lock')
    return sorted(paths)


def queue_candidates(cfg):
    """Known and leftover queues; no database access, credential loading or writes."""
    root = Path(cfg['queue']['file'])
    paths = {root, *(Path(e['config']['queue']['file']) for e in cfg.get('instances', []))}
    parent = root.parent/'instances'
    if parent.is_symlink():
        raise ValueError('unsafe_instance_runtime_directory')
    if parent.exists():
        suffix = '.dry-run' if cfg.get('ui', {}).get('dry_run') else ''
        paths.update(parent.glob('*/queue.sqlite3'+suffix))
    return sorted(paths)
