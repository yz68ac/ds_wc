"""Discover independent instance directories while retaining shared account resources."""
from __future__ import annotations

from pathlib import Path
import re


INSTANCE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z')
CHAT_ID = re.compile(r'(?:[a-zA-Z0-9_-]{1,128}@chatroom|wxid_[a-zA-Z0-9_-]{1,120})\Z')
GROUP_FIELDS = {'system_prompt_file', 'target', 'model_clients', 'models', 'profile',
                'reply_style', 'participation', 'memory', 'search', 'vision', 'queue', 'image_generation'}


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


def enabled_instances(cfg):
    return [(name, value) for name, value in cfg.instances.items() if value.enabled]


def select_instance(cfg, identifier):
    try:
        return cfg.instances[identifier]
    except KeyError:
        raise ValueError('未知实例ID') from None


def runtime_locks(cfg):
    from wxbridge.config.models import InstanceConfig
    if isinstance(cfg, InstanceConfig):
        return sorted({str(Path(cfg.account.decrypted_dir)/'.listener.lock'),cfg.paths.queue_file+'.lock'})
    paths = {str(Path(cfg.account.decrypted_dir)/'.listener.lock'), cfg.account.queue_file+'.lock'}
    paths.update(active.paths.queue_file+'.lock' for _, active in enabled_instances(cfg))
    return sorted(paths)


def queue_candidates(cfg):
    """Known and leftover queues; no database or provider access."""
    root = Path(cfg.account.queue_file)
    paths = {root, *(Path(value.paths.queue_file) for value in cfg.instances.values())}
    parent = root.parent/'instances'
    if parent.is_symlink():
        raise ValueError('unsafe_instance_runtime_directory')
    if parent.exists():
        suffix = '.dry-run' if cfg.account.ui.dry_run else ''
        paths.update(parent.glob('*/queue.sqlite3'+suffix))
    return sorted(paths)
