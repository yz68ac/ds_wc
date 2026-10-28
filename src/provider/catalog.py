"""Persist discovered model IDs without persisting connection secrets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .adapters import ModelError
from .discovery import DiscoveredModel, MAX_MODELS

MAX_CATALOG_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CatalogEntry:
    source_fingerprint: str
    fetched_at: str
    models: tuple[DiscoveredModel, ...]


@dataclass(frozen=True)
class ModelSyncResult:
    updated: dict[str, int]
    errors: dict[str, str]
    skipped: tuple[str, ...]


def source_fingerprint(config, provider):
    """Bind IDs to the endpoint/account configuration without saving its values."""
    connection = config.connections[provider]
    values = [provider, connection.base_url, connection.proxy_url,
              config.protocols[provider], dict(config.options[provider]), config.api_key(provider)]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode('utf-8')).hexdigest()


def make_entry(config, provider, models):
    return CatalogEntry(source_fingerprint(config, provider), datetime.now(timezone.utc).isoformat(), tuple(models))


def matching_catalog_models(config, entries):
    return {provider: entry.models for provider, entry in entries.items()
            if provider in config.connections and config.api_key(provider)
            and entry.source_fingerprint == source_fingerprint(config, provider)}


def load_catalog(path: Path, config) -> dict[str, CatalogEntry]:
    if not path.exists():
        return {}
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError()
        data = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'providers'}
                or type(data['schema']) is not int or data['schema'] not in (1, 2)
                or not isinstance(data['providers'], dict)
                or set(data['providers']) - set(config.connections)):
            raise ValueError()
        result = {}
        fingerprint_field = 'source' if data['schema'] == 1 else 'source_fingerprint'
        for provider, entry in data['providers'].items():
            if (not isinstance(entry, dict) or set(entry) != {fingerprint_field, 'fetched_at', 'models'}
                    or not isinstance(entry[fingerprint_field], str) or not re.fullmatch(r'[a-f0-9]{64}', entry[fingerprint_field])
                    or not isinstance(entry['fetched_at'], str) or not isinstance(entry['models'], list)
                    or len(entry['models']) > MAX_MODELS):
                raise ValueError()
            datetime.fromisoformat(entry['fetched_at'])
            models, seen = [], set()
            for row in entry['models']:
                if not isinstance(row, dict) or set(row) - {'id', 'display_name'}:
                    raise ValueError()
                identifier, display_name = row.get('id'), row.get('display_name')
                if (not isinstance(identifier, str) or not identifier or len(identifier) > 512
                        or any(ord(c) < 32 for c in identifier) or identifier in seen
                        or (display_name is not None and not isinstance(display_name, str))):
                    raise ValueError()
                seen.add(identifier)
                models.append(DiscoveredModel(provider, identifier, display_name))
            result[provider] = CatalogEntry(entry[fingerprint_field], entry['fetched_at'], tuple(models))
        return result
    except (ValueError, UnicodeError, TypeError):
        raise ModelError('invalid_model_catalog') from None


def save_catalog(path: Path, entries):
    data = {'schema': 2, 'providers': {
        provider: {'source_fingerprint': entry.source_fingerprint, 'fetched_at': entry.fetched_at,
                   'models': [{'id': model.id, 'display_name': model.display_name} for model in entry.models]}
        for provider, entry in sorted(entries.items())}}
    body = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(body) > MAX_CATALOG_BYTES:
        raise ModelError('model_list_too_large')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.models-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
