"""Typed role settings translated only at the provider boundary."""
from dataclasses import dataclass
from provider import load_config, ImageRequest
from wxbridge.config import InstanceConfig, TextOptions, ImageOptions, json_value

ROLES = ('reply', 'participation', 'memory', 'avatar', 'group_profile')
OPTIONS = {'max_tokens', 'temperature', 'timeout_seconds', 'thinking', 'reasoning_effort', 'thinking_mode', 'thinking_budget_tokens'}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    model: str
    options: TextOptions | ImageOptions
    provider: str
    base_url: str
    connection: str


def model_settings(cfg: InstanceConfig, role='reply', *, provider_config=None):
    if role not in (*ROLES, 'image'):
        raise ValueError('unknown_model_role')
    lane = role if role in ('reply', 'image') else 'background'
    config = provider_config if provider_config is not None else load_config(cfg.account.provider_env)
    connection = getattr(cfg.model_clients, lane)
    settings = getattr(cfg.models, role)
    protocol, base_url = config.protocols[connection], config.connections[connection].base_url
    if role == 'image' and config.options[connection].get('image_protocol') == 'gemini':
        protocol, base_url = 'gemini', config.options[connection].get('image_base_url', '')
    return ResolvedModel(settings.model, settings.options, protocol, base_url, connection)


def provider_settings(settings):
    return dict(json_value(settings.options), model=settings.model, provider=settings.provider,
                base_url=settings.base_url, connection=settings.connection)


def explain_config(cfg):
    return dict(instance=cfg.id, models={role:dict(settings=provider_settings(model_settings(cfg,role)))
                                      for role in (*ROLES,'image')}, clients=json_value(cfg.model_clients))


def image_request(cfg, prompt):
    return ImageRequest(cfg.models.image.model, prompt, json_value(cfg.models.image.options))
