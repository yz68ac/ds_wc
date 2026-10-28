"""Provider-neutral model invocation, independent of WeChat and bridge roles."""
from .adapters import ModelError, ModelRequest, ModelResult
from .client import Client, ModelClient, capture_metrics
from .factory import create_client
from .registry import Connection, ModelRegistry, ModelSpec
from .router import ModelRouter, ProviderClient
from .config import ProviderConfig, load_config
from .discovery import DiscoveredModel
from .catalog import ModelSyncResult
from .images import ImageRequest, ImageResult, GeneratedImage

__all__ = ['Connection', 'ModelClient', 'ModelError', 'ModelRegistry', 'ModelRequest',
           'ModelResult', 'ModelSpec', 'ProviderClient', 'ProviderConfig', 'DiscoveredModel',
           'ModelSyncResult', 'ModelRouter', 'Client', 'create_client', 'capture_metrics', 'load_config',
           'ImageRequest', 'ImageResult', 'GeneratedImage']
