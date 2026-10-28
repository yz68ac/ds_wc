"""Compile group overrides; account resources are never owned by a group."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re

from wxbridge.config_merge import deep_merge, field_origins

INSTANCE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z')
CHAT_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}@chatroom\Z')
OVERRIDES = {'system_prompt_file', 'target', 'model_clients', 'models',
             'reply_style', 'proactive', 'memory', 'search', 'vision', 'queue'}


def compile_instances(cfg, entries, path, *, resolver):
    account = cfg.get('account', {})
    if not isinstance(account, dict) or set(account)-{'send_interval_seconds'}:
        raise ValueError('account 仅支持 send_interval_seconds')
    gap = account.get('send_interval_seconds', 1)
    if type(gap) not in (int, float) or not math.isfinite(gap) or not 0 <= gap <= 60:
        raise ValueError('account.send_interval_seconds 必须为0–60秒')
    cfg['account'] = dict(send_interval_seconds=gap)
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError('instances 必须为最多16项的列表')
    if entries and os.environ.get('WX_GROUP'):
        raise ValueError('多群模式请删除 WX_GROUP，使用 instances[].chat')
    base = copy.deepcopy(cfg)
    base['instances'] = []
    base.pop('_compiled_models', None)
    base.pop('_config_origins', None)
    result, ids, chats, legacy = [], set(), set(), False
    for entry in entries:
        if not isinstance(entry, dict) or set(entry)-{'id', 'chat', 'enabled', 'reuse_legacy_state', 'overrides'}:
            raise ValueError('instances 项仅支持 id/chat/enabled/reuse_legacy_state/overrides')
        identifier, chat = entry.get('id'), entry.get('chat')
        if not isinstance(identifier, str) or not INSTANCE_ID.fullmatch(identifier) or identifier.lower() in ids:
            raise ValueError('instances.id 无效或重复（不区分大小写）')
        if not isinstance(chat, str) or not CHAT_ID.fullmatch(chat) or chat in chats:
            raise ValueError('instances.chat 必须为不重复的稳定群ID')
        ids.add(identifier.lower()); chats.add(chat)
        for field in ('enabled', 'reuse_legacy_state'):
            if type(entry.get(field, field == 'enabled')) is not bool:
                raise ValueError('instances.'+field+' 必须为布尔值')
        reuse = entry.get('reuse_legacy_state', False)
        if reuse and (legacy or not cfg['target']['chat']):
            raise ValueError('仅一个实例可 reuse_legacy_state，且须保留原 target.chat')
        legacy |= reuse
        override = entry.get('overrides', {})
        if not isinstance(override, dict) or set(override)-OVERRIDES:
            raise ValueError('instances.overrides 包含账号级或未知配置')
        for section in OVERRIDES-{'system_prompt_file'}:
            if section in override and not isinstance(override[section], dict):
                raise ValueError('instances.overrides.'+section+' 必须为对象')
        for section, forbidden in (('target', {'chat'}), ('queue', {'file'}),
                                   ('memory', {'directory'})):
            if section in override and (not isinstance(override[section], dict) or set(override[section]) & forbidden):
                raise ValueError('instances.overrides.'+section+' 不可覆盖共享资源或身份')
        effective = deep_merge(base, override)
        origins = {**cfg.get('_config_origins', {}),
                   **field_origins(override, 'instance.'+identifier)}
        effective['target']['chat'] = chat
        if not reuse:
            runtime = Path(cfg['queue']['file']).parent/'instances'/identifier
            effective['queue']['file'] = str(runtime/'queue.sqlite3')
            effective['state_file'] = str(runtime/'state.json')
        effective = resolver(effective, path, environment=False, origins=origins)
        effective['instances'] = []
        effective['_instance_id'] = identifier
        result.append(dict(id=identifier, chat=chat, enabled=entry.get('enabled', True),
                           reuse_legacy_state=reuse, config=effective))
    if entries and not any(e['enabled'] for e in result):
        raise ValueError('instances 至少启用一个实例')
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
