"""Read, validate and resolve independent account and instance snapshots."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path

from wxbridge.config_merge import deep_merge, field_origins
from wxbridge.paths import resource_path
from .models import AppConfig, AccountConfig, ConfigError, ConfigSource, FrozenMap, RuntimePaths, RuntimePolicy, InstanceConfig
from .codec import (account_document, account_from_document, default_document, instance_from_document,
                    configuration_hash)
from .validation import validate_document


def _read(path, *, missing=False):
    raw = path.read_bytes() if path.exists() else b'{}' if missing else path.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ConfigError('invalid_json') from None
    if not isinstance(data, dict):
        raise ConfigError('configuration_must_be_object')
    return data, hashlib.sha256(raw).hexdigest()


def _validated(document, path, **options):
    try:
        return validate_document(document, path, **options)
    except ConfigError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        # Existing validators use fixed codes/field names, never input values.
        code = str(exc) if isinstance(exc, ValueError) else 'invalid_config_type'
        raise ConfigError(code) from None


def load_account(path='config.json') -> AccountConfig:
    from wxbridge.instances import validate_account_fields
    path = Path(path).resolve()
    document, revision = _read(path, missing=True)
    validate_account_fields(document)
    account = document.get('account', {})
    if not isinstance(account, dict) or set(account)-{'send_interval_seconds'}:
        raise ConfigError('account only accepts send_interval_seconds', 'account')
    gap = account.get('send_interval_seconds', 1)
    if type(gap) not in (int, float) or not math.isfinite(gap) or not 0 <= gap <= 60:
        raise ConfigError('account.send_interval_seconds must be 0..60', 'account.send_interval_seconds')
    resolved = _validated(document, path, validate_role_models=False)
    source = ConfigSource(str(path), revision, FrozenMap(tuple(resolved.pop('_config_origins').items())))
    value = account_from_document(resolved, metadata=source)
    return value


def resolve_instance(account, document, *, path):
    from wxbridge.instances import INSTANCE_ID, CHAT_ID, GROUP_FIELDS
    path = Path(path).resolve()
    identifier = path.parent.name
    if not INSTANCE_ID.fullmatch(identifier):
        raise ConfigError('invalid_instance_id')
    if not isinstance(document, dict) or set(document)-GROUP_FIELDS-{'chat', 'enabled'}:
        raise ConfigError('invalid_instance_fields')
    enabled, chat = document.get('enabled', True), document.get('chat', '')
    if type(enabled) is not bool or not isinstance(chat, str):
        raise ConfigError('invalid_instance_identity')
    if (enabled and not CHAT_ID.fullmatch(chat)) or chat and not CHAT_ID.fullmatch(chat):
        raise ConfigError('instance_chat_requires_unique_stable_id')
    override = {key: value for key, value in document.items() if key in GROUP_FIELDS}
    for section in GROUP_FIELDS-{'system_prompt_file'}:
        if section in override and not isinstance(override[section], dict):
            raise ConfigError('invalid_instance_section', section)
    for section, forbidden in (('target', {'chat'}), ('queue', {'file'}), ('memory', {'directory'})):
        if set(override.get(section, {})) & forbidden:
            raise ConfigError('instance_cannot_override_account_resource', section)
    if 'system_prompt_file' in override:
        value = override['system_prompt_file']
        if not isinstance(value, str) or not value.strip():
            raise ConfigError('invalid_system_prompt_file')
        override['system_prompt_file'] = str((path.parent/value).resolve())
    else:
        override['system_prompt_file'] = str(resource_path(InstanceConfig().system_prompt_file).resolve())
    if 'system_prompt_file' in override.get('memory', {}):
        override = deep_merge({}, override)
        override['memory']['system_prompt_file'] = str((path.parent/override['memory']['system_prompt_file']).resolve())
    effective = deep_merge(account_document(account), override)
    effective.setdefault('target', {})['chat'] = chat
    runtime = Path(account.queue_file).parent/'instances'/identifier
    effective['queue']['file'] = str(runtime/'queue.sqlite3')
    effective['state_file'] = str(runtime/'state.json')
    origins = {**dict(account.metadata.origins), **field_origins(override, 'instance.'+identifier)}
    resolved = _validated(effective, path, environment=False, origins=origins, validate_role_models=enabled)
    source = ConfigSource(str(path), origins=FrozenMap(tuple(resolved.pop('_config_origins').items())))
    value = instance_from_document(resolved, account=account, identifier=identifier, enabled=enabled, metadata=source)
    return value


def load_config(path='config.json') -> AppConfig:
    path = Path(path).resolve()
    account = load_account(path)
    if os.environ.get('WX_GROUP'):
        raise ConfigError('WX_GROUP is retired; use instance/<id>/config.json chat')
    directory = path.parent/'instance'
    if directory.is_symlink():
        raise ConfigError('unsafe_instance_directory')
    files = sorted(directory.glob('*/config.json')) if directory.exists() else []
    if len(files)>16:
        raise ConfigError('too_many_instances')
    instances, identifiers, chats = [], set(), set()
    for file in files:
        if file.is_symlink() or file.parent.is_symlink():
            raise ConfigError('unsafe_instance_config')
        document, revision = _read(file)
        value = resolve_instance(account, document, path=file)
        if value.id.casefold() in identifiers:
            raise ConfigError('invalid_or_duplicate_instance_id')
        if value.chat and value.chat in chats:
            raise ConfigError('instance_chat_requires_unique_stable_id')
        identifiers.add(value.id.casefold())
        if value.chat:
            chats.add(value.chat)
        value = replace(value, metadata=replace(value.metadata, revision=revision))
        instances.append((value.id, value))
    app = AppConfig(account=account, instances=FrozenMap(tuple(instances)), metadata=account.metadata)
    return app


def prepare_runtime(app, *, dry_run=None, keys_file=None):
    """Construct overrides without changing desired snapshots or their revisions."""
    if app.runtime.prepared and (dry_run is None or dry_run == app.account.ui.dry_run) and keys_file is None:
        return app
    account = app.account
    ui = replace(account.ui, dry_run=account.ui.dry_run if dry_run is None else dry_run,
                 verify_chat_selected=True)
    account = replace(account, ui=ui, keys_file=account.keys_file if keys_file is None else str(keys_file))
    def runtime_paths(paths):
        if not ui.dry_run:
            return paths
        return replace(paths, queue_file=paths.queue_file+'.dry-run',
                       state_file=str(Path(paths.state_file).parent/'dry-run/state.json'))
    if isinstance(app, AppConfig):
        paths = runtime_paths(app.paths)
        account = replace(account, queue_file=paths.queue_file, state_file=paths.state_file)
        instances = FrozenMap(tuple((name, replace(value, account=account, paths=runtime_paths(value.paths), runtime=RuntimePolicy(prepared=True, desired_fingerprint=configuration_hash(value))))
                                    for name, value in app.instances.items()))
        return replace(app, account=account, instances=instances, runtime=RuntimePolicy(prepared=True, desired_fingerprint=configuration_hash(app)))
    return replace(app, account=account, paths=runtime_paths(app.paths), runtime=RuntimePolicy(prepared=True, desired_fingerprint=configuration_hash(app)))
