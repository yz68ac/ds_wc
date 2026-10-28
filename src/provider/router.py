"""Route each request by explicit model alias."""
import os
from pathlib import Path
from dataclasses import replace

from .adapters import ModelError, get_protocol_adapter


class ModelRouter:
    def __init__(self, registry, *, clients=None):
        self.registry = registry
        self._clients = dict(clients or {})
        self._config = None
        self._discovered = {}
        self._catalog_path = None
        self._catalog_entries = {}

    @classmethod
    def from_env(cls, path=None, *, environ=None, models_path=None, clients=None):
        from .catalog import matching_catalog_models, load_catalog
        from .config import env_path, load_config
        from .registry import ModelRegistry
        config = load_config(path, environ=environ)
        catalog_path = Path(models_path) if models_path is not None else env_path(path).with_name('models.json')
        catalog_path = catalog_path.resolve()
        entries = load_catalog(catalog_path, config)
        discovered = matching_catalog_models(config, entries)
        client = cls(ModelRegistry.from_discovered(config,
            (model for models in discovered.values() for model in models)), clients=clients)
        client._config = config
        client._catalog_path = catalog_path
        client._catalog_entries = entries
        client._discovered = discovered
        return client

    def _replace_catalog(self, entries):
        from .catalog import matching_catalog_models, save_catalog
        from .registry import ModelRegistry
        discovered = matching_catalog_models(self._config, entries)
        registry = ModelRegistry.from_discovered(self._config,
            (model for models in discovered.values() for model in models))
        save_catalog(self._catalog_path, entries)
        self._catalog_entries = entries
        self._discovered = discovered
        self.registry = registry

    @property
    def catalog_path(self):
        return self._catalog_path

    def refresh_models(self, provider, *, timeout=30):
        """Refresh one provider and atomically persist the updated catalog."""
        from .catalog import make_entry
        if self._config is None:
            raise ModelError('discovery_not_configured')
        found = self._connection_client(provider).fetch_models(timeout=timeout)
        entries = {**self._catalog_entries, provider: make_entry(self._config, provider, found)}
        self._replace_catalog(entries)
        return found

    def sync_models(self, *, timeout=30):
        """Discover every configured provider; keep old entries for failed requests."""
        from .catalog import ModelSyncResult, make_entry
        if self._config is None:
            raise ModelError('discovery_not_configured')
        configured = [name for name in self._config.connections if self._config.api_key(name)]
        if not configured:
            raise ModelError('no_configured_providers')
        entries, updated, errors = dict(self._catalog_entries), {}, {}
        for provider in configured:
            try:
                found = self._connection_client(provider).fetch_models(timeout=timeout)
            except ModelError as exc:
                errors[provider] = exc.code
            else:
                entries[provider] = make_entry(self._config, provider, found)
                updated[provider] = len(found)
        if updated:
            self._replace_catalog(entries)
        skipped = tuple(name for name in self._config.connections if name not in configured)
        return ModelSyncResult(updated, errors, skipped)

    def list_cached_models(self, provider=None):
        """Return the last successfully refreshed snapshot without network access."""
        if provider is not None:
            if self._config is not None and provider not in self._config.connections:
                raise ModelError('unsupported_provider')
            return tuple(self._discovered.get(provider, ()))
        return tuple(model for models in self._discovered.values() for model in models)

    # Compatibility alias; this only reads the loaded local catalog.
    list_models = list_cached_models

    def _connection_client(self, name, settings=None):
        client = self._clients.get(name)
        if client is None:
            raise ModelError('client_not_configured')
        if settings is None:
            if self._config is None or name not in self._config.connections:
                raise ModelError('unsupported_provider')
            connection = self._config.connections[name]
            settings = {**self._config.options[name], 'provider': self._config.protocols[name],
                        'base_url': connection.base_url, 'api_key_env': connection.api_key_env,
                        'proxy_url': connection.proxy_url}
        key = (self._config.api_key(name) if self._config is not None
               else os.environ.get(settings['api_key_env'], '').strip())
        client.check_connection(settings, api_key=key)
        return client

    def generate(self, request, *, deadline=None, require_complete=False):
        if not request.model:
            raise ModelError('missing_model')
        settings = self.registry.resolve(request.model)
        name = self.registry.connection_name(request.model)
        client = self._connection_client(name, settings)
        return client._generate(request, settings, deadline=deadline, require_complete=require_complete)

    def generate_images(self, request, *, deadline=None):
        if not request.model:
            raise ModelError('missing_model')
        settings = self.registry.resolve(request.model)
        name = self.registry.connection_name(request.model)
        client = self._connection_client(name, settings)
        return client.generate_images(replace(request, model=settings['model']), deadline=deadline)

    def assistant_message(self, model, message):
        settings = self.registry.resolve(model)
        result = get_protocol_adapter(settings['provider']).assistant_message(message)
        if '_provider_route' in message:
            result['_provider_route'] = message['_provider_route']
        return result

    def stream(self, request, *, deadline=None):
        # Do not simulate streaming from a completed response.
        raise ModelError('unsupported_stream')


# Compatibility name; the router never owns SDK clients.
ProviderClient = ModelRouter
