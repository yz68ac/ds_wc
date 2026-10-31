"""Bound request size and route supported model protocols to their SDKs."""
import json

from .adapters import ModelError
from .sdk_transport import send as sdk_send
from .sdk_transport import send_images as sdk_send_images

MAX_REQUEST_BYTES = 48 * 1024 * 1024

SUPPORTED_PROTOCOLS = frozenset({
    'deepseek', 'openai_compatible', 'openai', 'openai_responses',
    'anthropic', 'anthropic_messages', 'gemini',
})


def _check_request_size(payload):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_REQUEST_BYTES:
        raise ModelError('request_too_large')


def send(sdk, client, protocol, payload, timeout):
    _check_request_size(payload)
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ModelError('unsupported_provider')
    return sdk_send(sdk, client, protocol, payload, timeout)


def send_images(sdk, client, payload, timeout):
    _check_request_size(payload)
    return sdk_send_images(sdk, client, payload, timeout)
