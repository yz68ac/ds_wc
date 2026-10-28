"""Create one connection-bound client for the caller to own and reuse."""
from .adapters import ModelError
from .client import Client
from .config import load_config


def create_client(connection: str, *, config=None) -> Client:
    config = load_config() if config is None else config
    if connection not in config.connections:
        raise ModelError('unsupported_provider')
    endpoint = config.connections[connection]
    settings = {**config.options[connection], 'provider': config.protocols[connection],
                'base_url': endpoint.base_url, 'api_key_env': endpoint.api_key_env,
                'proxy_url': endpoint.proxy_url, 'thinking': 'disabled'}
    return Client(settings, api_key=config.api_key(connection), name=connection)
