"""Explicit synthetic configuration for business fixtures; no real credentials."""
from copy import deepcopy
from pathlib import Path
from provider import Client, ModelError, ModelResult, capture_metrics
from wxbridge.config import DEFAULTS
from wxbridge.ai.roles import model_settings

def default_config():
    cfg=deepcopy(DEFAULTS)
    cfg['provider_env']=str(Path(__file__).parent/'.synthetic-provider.env')
    cfg['target']['bot_name']='DeepSeek'
    cfg['participation'].update(enabled=True, debounce_seconds=0, address_names=['ds', 'deepseek'])
    for role in cfg['models']: cfg['models'][role]['model']='synthetic-model'
    return cfg

class ClientFixture(Client):
    def __init__(self,cfg,*,role='reply',adapter=None):
        settings=model_settings(cfg,role)
        super().__init__(settings,api_key='synthetic-key',role=role,adapter=adapter)
