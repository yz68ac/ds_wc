"""Explicit document migrations; ordinary loading never invokes these."""
from copy import deepcopy
import math
from typing import Any
from .codec import default_document

def migrate_context_data(data: dict[str, Any]) -> dict[str, Any]:
    """Explicit conversion only; loading rejects the retired field."""
    if not isinstance(data, dict):
        raise ValueError('instance_config_must_be_object')
    result = deepcopy(data)
    target = result.get('target', {})
    if not isinstance(target, dict):
        raise ValueError('target 必须为 JSON 对象')
    if 'context_window_seconds' in target:
        seconds = target.pop('context_window_seconds')
        if type(seconds) is not int or seconds < 1:
            raise ValueError('invalid_legacy_context_window')
        memory = result.setdefault('memory', {})
        if not isinstance(memory, dict):
            raise ValueError('memory 必须为 JSON 对象')
        if 'context_window_seconds' in memory and memory['context_window_seconds'] != seconds:
            raise ValueError('conflicting_memory_context_window')
        memory['context_window_seconds'] = seconds
    return result

def migrate_instance_data(data: dict[str, Any]) -> dict[str, Any]:
    """Explicit one-time JSON conversion; normal loading never calls this helper.

    Return an independent document and preserve identity, enable state, paths,
    models and unknown business fields. The caller chooses where to save it.
    """
    if not isinstance(data, dict):
        raise ValueError('instance_config_must_be_object')
    result = migrate_context_data(data)
    old = result.pop('proactive', None)
    target, queue = result.get('target', {}), result.get('queue', {})
    if not isinstance(target, dict) or not isinstance(queue, dict) or (old is not None and not isinstance(old, dict)):
        raise ValueError('invalid_legacy_participation_configuration')
    trigger = target.pop('trigger', None)
    if trigger is not None and not isinstance(trigger, dict):
        raise ValueError('invalid_legacy_participation_configuration')
    debounce = queue.pop('debounce_seconds', None)
    if old is None and trigger is None and debounce is None:
        return result
    existing = result.get('participation', {})
    if not isinstance(existing, dict):
        raise ValueError('invalid_participation_settings')
    migrated = default_document()['participation']
    if old is not None:
        migrated['enabled'] = old.get('enabled', False)
        opportunities = old.get('opportunities', {})
        if not isinstance(opportunities, dict):
            raise ValueError('invalid_legacy_participation_configuration')
        names = opportunities.get('address_names', [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('invalid_legacy_participation_configuration')
        migrated['address_names'] = list(names)
        if 'window_seconds' in old:
            result.setdefault('memory', {}).setdefault('context_window_seconds', old['window_seconds'])
    if trigger is not None:
        migrated['pat_enabled'] = trigger.get('pat_enabled', True)
        names = trigger.get('mentions', [])
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('invalid_legacy_participation_configuration')
        migrated['address_names'] += [n.lstrip('@') for n in names if n.lstrip('@').strip()]
        migrated['address_names'] = list(dict.fromkeys(migrated['address_names']))
    if debounce is not None:
        migrated['debounce_seconds'] = debounce
        if type(debounce) in (int, float) and math.isfinite(debounce):
            migrated['max_wait_seconds'] = max(migrated['max_wait_seconds'], debounce)
    migrated.update(existing)
    result['participation'] = migrated
    return result
