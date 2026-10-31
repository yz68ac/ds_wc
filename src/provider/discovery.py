"""Discover account-visible model IDs via the providers' SDKs."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .adapters import ModelError
from .sdk_transport import map_sdk_error

MAX_PAGES = 100
MAX_MODELS = 10000


@dataclass(frozen=True)
class DiscoveredModel:
    provider: str
    id: str
    display_name: str | None = None

    @property
    def alias(self) -> str:
        return self.provider + '/' + self.id


def fetch_models(sdk, client, provider: str, *, timeout=30, protocol=None) -> tuple[DiscoveredModel, ...]:
    """Fetch using an existing SDK client; ownership stays with its creator."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ModelError('invalid_timeout')
    found, seen = [], set()
    try:
        gemini = protocol == 'gemini'
        page = (client.models.list(config=dict(page_size=100, http_options=dict(timeout=int(timeout*1000))))
                if gemini else client.models.list(timeout=timeout))
        for page_number in range(MAX_PAGES):
            data = page.page if gemini else page.data
            if not isinstance(data, list):
                raise ModelError('invalid_response')
            for model in data:
                identifier = getattr(model, 'name' if gemini else 'id', None)
                if gemini and isinstance(identifier, str) and identifier.startswith('models/'):
                    identifier = identifier[len('models/'):]
                if (not isinstance(identifier, str) or not identifier or len(identifier) > 512
                        or any(ord(c) < 32 for c in identifier)):
                    raise ModelError('invalid_response')
                if identifier not in seen:
                    seen.add(identifier)
                    display = getattr(model, 'display_name', None)
                    found.append(DiscoveredModel(provider, identifier,
                                  display if isinstance(display, str) else None))
                    if len(found) > MAX_MODELS:
                        raise ModelError('model_list_too_large')
            more = bool(page.config.get('page_token')) if gemini else page.has_next_page()
            if not more:
                return tuple(found)
            if page_number + 1 == MAX_PAGES:
                raise ModelError('model_list_too_large')
            if gemini:page.next_page()
            else:page = page.get_next_page()
    except (ValueError, UnicodeError, AttributeError, TypeError):
        raise ModelError('invalid_response') from None
    except Exception as exc:
        mapped = map_sdk_error(sdk, exc)
        if mapped is not None:
            raise mapped from None
        raise
