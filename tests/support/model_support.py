"""Explicit synthetic configuration for business fixtures; no real credentials."""
from copy import deepcopy
from pathlib import Path
from provider import Client, ModelError, ModelResult, capture_metrics
from wxbridge.config import default_document
from wxbridge.ai.roles import model_settings, provider_settings
from tests.support.configuration import snapshot

__all__ = ['default_config', 'ClientFixture', 'ModelError', 'ModelResult', 'capture_metrics']

def default_config():
    cfg=default_document()
    cfg['provider_env']=str(Path(__file__).parent/'.synthetic-provider.env')
    cfg['target']['bot_name']='DeepSeek'
    cfg['participation'].update(enabled=True, debounce_seconds=0, address_names=['ds', 'deepseek'])
    for role in cfg['models']: cfg['models'][role]['model']='synthetic-model'
    return snapshot(cfg)

class ClientFixture(Client):
    def __init__(self,cfg,*,role='reply',adapter=None):
        settings=provider_settings(model_settings(cfg,role))
        super().__init__(settings,api_key='synthetic-key',role=role,adapter=adapter)
