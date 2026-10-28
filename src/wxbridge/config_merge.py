"""Pure configuration merge and provenance helpers; no environment or I/O."""
from __future__ import annotations

import copy


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def field_origins(value: dict, source: str, prefix: str = '') -> dict[str, str]:
    """Leaf paths identify the winning layer without retaining any values."""
    result = {}
    for key, item in value.items():
        path = prefix+'.'+key if prefix else key
        if isinstance(item, dict):
            result.update(field_origins(item, source, path))
        else:
            result[path] = source+':'+path
    return result
