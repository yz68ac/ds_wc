"""Explicit model-to-protocol and connection routing, independent of the bridge."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Mapping
from urllib.parse import urlsplit

from .adapters import ModelError, get_protocol_adapter


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key_env: str
    proxy_url: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    protocol: str
    upstream_model: str
    connection: str
    options: Mapping = field(default_factory=dict, repr=False)


class ModelRegistry:
    def __init__(self, models: Mapping[str, ModelSpec], connections: Mapping[str, Connection], *, ambiguous=()):
        self._models = deepcopy(dict(models))
        self._connections = deepcopy(dict(connections))
        self._ambiguous = frozenset(ambiguous)
        for name, spec in self._models.items():
            if not isinstance(name, str) or not name or not isinstance(spec, ModelSpec):
                raise ValueError('invalid_model_spec')
            get_protocol_adapter(spec.protocol)
            if (not isinstance(spec.upstream_model, str) or not spec.upstream_model.strip()
                    or not isinstance(spec.connection, str) or spec.connection not in self._connections
                    or not isinstance(spec.options, Mapping)):
                raise ValueError('invalid_model_spec')
            connection = self._connections[spec.connection]
            if not isinstance(connection, Connection) or not isinstance(connection.base_url, str) or not isinstance(connection.api_key_env, str):
                raise ValueError('invalid_connection')
            try:
                url = urlsplit(connection.base_url)
                if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                        or url.query or url.fragment or (url.port is not None and not 1 <= url.port <= 65535)
                        or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', connection.api_key_env)):
                    raise ValueError('invalid_connection')
                if connection.proxy_url is not None:
                    proxy = urlsplit(connection.proxy_url)
                    if (proxy.scheme != 'http' or not proxy.hostname or proxy.username or proxy.password
                            or proxy.path not in ('', '/') or proxy.query or proxy.fragment
                            or any(ord(c) <= 32 for c in connection.proxy_url)
                            or (proxy.port is not None and not 1 <= proxy.port <= 65535)):
                        raise ValueError('invalid_connection')
            except (TypeError, ValueError):
                raise ValueError('invalid_connection') from None
            if set(spec.options) & {'provider', 'model', 'base_url', 'api_key_env', 'proxy_url'}:
                raise ValueError('reserved_model_option')

    def resolve(self, model: str) -> dict:
        if isinstance(model, str) and model in self._ambiguous:
            raise ModelError('ambiguous_model')
        try:
            spec = self._models[model]
        except (KeyError, TypeError):
            raise ModelError('unknown_model') from None
        connection = self._connections[spec.connection]
        settings = deepcopy(dict(spec.options))
        settings.update(provider=spec.protocol, model=spec.upstream_model,
                        base_url=connection.base_url, api_key_env=connection.api_key_env)
        if connection.proxy_url is not None:
            settings['proxy_url'] = connection.proxy_url
        return settings

    def connection_name(self, model: str) -> str:
        self.resolve(model)
        return self._models[model].connection

    @classmethod
    def from_discovered(cls, config, discovered):
        models, by_id = {}, {}
        for model in discovered:
            spec = ModelSpec(config.protocols[model.provider], model.id, model.provider,
                             {**config.options[model.provider], 'thinking': 'disabled'})
            if model.alias in models:
                raise ValueError('duplicate_model_alias')
            models[model.alias] = spec
            by_id.setdefault(model.id, []).append(spec)
        ambiguous = set()
        for identifier, specs in by_id.items():
            if len(specs) == 1 and identifier not in models:
                models[identifier] = specs[0]
            elif len(specs) > 1 and identifier not in models:
                ambiguous.add(identifier)
        return cls(models, config.connections, ambiguous=ambiguous)

    @classmethod
    def from_profiles(cls, profiles: Mapping[str, Mapping]):
        """Adapt the existing profile format without inheriting bridge defaults."""
        models, connections = {}, {}
        for name, profile in profiles.items():
            data = dict(profile)
            protocol = data.pop('provider')
            model = data.pop('model')
            base_url = data.pop('base_url')
            key_env = data.pop('api_key_env')
            proxy = data.pop('proxy_url', None)
            connections[name] = Connection(base_url, key_env, proxy)
            models[name] = ModelSpec(protocol, model, name, data)
        return cls(models, connections)
