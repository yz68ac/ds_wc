"""Explicit JSON boundaries for immutable configuration objects."""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json

from .models import (
    AccountConfig, AppConfig, ConfigError, FrozenMap, InstanceConfig, Missing,
    RuntimePaths, Section, UNSET, freeze,
)

INSTANCE_SECTIONS = ('target', 'model_clients', 'models', 'profile', 'reply_style',
                     'participation', 'memory', 'search', 'vision', 'queue', 'image_generation')
ACCOUNT_FIELDS = ('db_dir', 'keys_file', 'decrypted_dir', 'state_file', 'log_file',
                  'wechat_process', 'source', 'poll_interval_seconds', 'provider_env')


def model_items(models):
    return tuple((name, getattr(models, name)) for name in ('image', 'reply', 'participation', 'memory', 'avatar', 'group_profile'))


def channel_items(clients):
    return tuple((name, getattr(clients, name)) for name in ('reply', 'image', 'background'))


def json_value(value):
    if isinstance(value, Section):
        result = {item.name: json_value(getattr(value, item.name)) for item in fields(value)
                  if item.name not in {'extensions', 'metadata', 'account', 'paths', 'runtime'}
                  and getattr(value, item.name) is not UNSET}
        return {**json_value(value.extensions), **result}
    if isinstance(value, FrozenMap):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, Missing):
        raise ConfigError('unset_config_value')
    return value


def section_from_document(cls, document, *, path=''):
    if not isinstance(document, dict):
        raise ConfigError('invalid_config_section', path)
    template, values = cls(), {}
    known = {item.name for item in fields(cls)} - {'extensions'}
    for name, value in document.items():
        if name not in known:
            continue
        default = getattr(template, name)
        values[name] = (section_from_document(type(default), value, path=path+'.'+name)
                        if isinstance(default, Section) else freeze(value))
    values['extensions'] = freeze({key: value for key, value in document.items() if key not in known})
    try:
        return cls(**values)
    except ConfigError as exc:
        leaf = exc.path.partition('.')[2]
        raise ConfigError(exc.code, '.'.join(part for part in (path, leaf) if part) or exc.path) from None


def account_document(account: AccountConfig):
    return {**json_value(account.extensions), **{name: getattr(account, name) for name in ACCOUNT_FIELDS},
            'ui': json_value(account.ui), 'native_api': json_value(account.native_api),
            'account': {'send_interval_seconds': account.send_interval_seconds},
            'queue': {'file': account.queue_file}, 'memory': {'directory': account.memory_directory}}


def instance_document(instance: InstanceConfig):
    return {**json_value(instance.extensions), 'chat': instance.chat, 'enabled': instance.enabled,
            'system_prompt_file': instance.system_prompt_file,
            **{name: json_value(getattr(instance, name)) for name in INSTANCE_SECTIONS}}


def effective_document(config: InstanceConfig):
    value = {**json_value(config.extensions), **account_document(config.account)}
    value.update({name: json_value(getattr(config, name)) for name in INSTANCE_SECTIONS})
    value['system_prompt_file'] = config.system_prompt_file
    value['target']['chat'] = config.chat
    value['queue']['file'] = config.paths.queue_file
    value['memory']['directory'] = config.paths.memory_directory
    value['state_file'] = config.paths.state_file
    value['reply_to_backlog'] = config.runtime.reply_to_backlog
    return value


def default_document():
    """A fresh export for examples and document editing, never a mutable global."""
    return effective_document(InstanceConfig())


def account_from_document(document, *, metadata=None):
    from .models import ConfigSource, NativeAPIConfig, UIConfig
    return AccountConfig(**{name: document[name] for name in ACCOUNT_FIELDS},
        queue_file=document['queue']['file'], memory_directory=document['memory']['directory'],
        send_interval_seconds=document.get('account', {}).get('send_interval_seconds', 1),
        ui=section_from_document(UIConfig, document['ui'], path='ui'),
        native_api=section_from_document(NativeAPIConfig, document['native_api'], path='native_api'),
        metadata=metadata or ConfigSource())


def instance_from_document(document, *, account=None, identifier='', enabled=True, metadata=None):
    from .models import ConfigSource
    template = InstanceConfig()
    sections = {}
    for name in INSTANCE_SECTIONS:
        data = dict(document[name])
        for key in {'target': ('chat',), 'memory': ('directory',), 'queue': ('file',)}.get(name, ()):
            data.pop(key, None)
        sections[name] = section_from_document(type(getattr(template, name)), data, path=name)
    return InstanceConfig(id=identifier, enabled=enabled, chat=document['target']['chat'],
        account=account or account_from_document(document),
        paths=RuntimePaths(document['queue']['file'], document['state_file'], document['memory']['directory']),
        system_prompt_file=document['system_prompt_file'], metadata=metadata or ConfigSource(), **sections)


def configuration_hash(config):
    if isinstance(config, (AppConfig, InstanceConfig)) and config.runtime.prepared:
        return config.runtime.desired_fingerprint
    if isinstance(config, AppConfig):
        document = {'account': account_document(config.account),
                    'instances': {name: {'enabled': item.enabled, **effective_document(item)}
                                  for name, item in config.instances.items()}}
    elif isinstance(config, AccountConfig):
        document = account_document(config)
    else:
        document = effective_document(config)
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()[:16]
