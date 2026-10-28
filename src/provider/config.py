"""Provider-local connections and credentials, without changing process environment."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping

from .registry import Connection, ModelRegistry, ModelSpec

def default_env_path() -> Path:
    package = Path(__file__).resolve().parent
    # Source/editable usage keeps the provider-local configuration. Installed
    # wheels have no adjacent build metadata and use the caller's project.
    directory = package if (package / 'pyproject.toml').is_file() else Path.cwd()
    return directory / '.env'


def env_path(path=None) -> Path:
    return (Path(path) if path is not None else default_env_path()).resolve()


PROVIDERS = ('openai', 'anthropic', 'deepseek')
DEFAULTS = {
    'openai': ('https://api.openai.com/v1', 'openai_responses'),
    'anthropic': ('https://api.anthropic.com/v1', 'anthropic_messages'),
    'deepseek': ('https://api.deepseek.com', 'deepseek'),
}


@dataclass(frozen=True)
class ProviderConfig:
    connections: Mapping[str, Connection]
    protocols: Mapping[str, str]
    options: Mapping[str, Mapping[str, str]]
    credentials: Mapping[str, str] = field(repr=False)

    def api_key(self, name: str) -> str:
        return self.credentials.get(name, '')


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or key in values:
            raise ValueError('invalid_provider_env')
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError('invalid_provider_env')
            value = value[1:-1]
        values[key] = value
    return values


def load_config(path: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> ProviderConfig:
    """Read the local .env; process variables take precedence, even if empty."""
    values = _read_env(env_path(path))
    environment = os.environ if environ is None else environ
    connections, protocols, credentials, options = {}, {}, {}, {}
    for name in PROVIDERS:
        prefix = name.upper()
        def get(suffix, default=''):
            key = prefix+'_'+suffix
            return environment[key] if key in environment else values.get(key, default)
        default_url, default_protocol = DEFAULTS[name]
        protocol = get('PROTOCOL', default_protocol).strip() or default_protocol
        allowed = {'openai': ('openai', 'openai_responses'),
                   'anthropic': ('anthropic', 'anthropic_messages'),
                   'deepseek': ('deepseek',)}
        if protocol not in allowed[name]:
            raise ValueError('invalid_provider_protocol')
        key_name = prefix+'_API_KEY'
        connections[name] = Connection(get('BASE_URL', default_url).strip() or default_url,
                                       key_name, get('PROXY_URL').strip() or None)
        protocols[name] = protocol
        credentials[name] = get('API_KEY').strip()
        options[name] = {}
        if name == 'anthropic':
            version = get('VERSION').strip()
            auth = get('AUTH').strip()
            if ((version and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', version))
                    or (auth and auth not in ('api_key', 'bearer'))):
                raise ValueError('invalid_provider_auth')
            if version:
                options[name]['anthropic_version'] = version
            if auth:
                options[name]['anthropic_auth'] = auth
        # Reuse the registry's endpoint and proxy validation before any request.
        ModelRegistry({name: ModelSpec(protocol, 'validation', name)}, {name: connections[name]})
    return ProviderConfig(connections, protocols, options, credentials)
