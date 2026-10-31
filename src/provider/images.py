"""Image generation contracts and OpenAI conversions; no file writes or downloads."""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass, field
import math
import re

from .adapters import ModelError

IMAGE_PROTOCOLS = frozenset({'openai', 'openai_responses', 'openai_compatible'})


@dataclass(frozen=True)
class ImageRequest:
    model: str
    prompt: str = field(repr=False)
    options: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    revised_prompt: str | None = field(default=None, repr=False)
    mime: str | None = None


@dataclass(frozen=True)
class ImageResult:
    images: tuple[GeneratedImage, ...] = field(repr=False)
    created: int | None = None
    usage: dict = field(default_factory=dict)
    output_format: str | None = None
    size: str | None = None
    quality: str | None = None
    background: str | None = None
    text: str = field(default='', repr=False)


def build_image_payload(request: ImageRequest):
    if not isinstance(request.model, str) or not request.model.strip():
        raise ModelError('missing_model')
    if not isinstance(request.prompt, str) or not request.prompt.strip() or len(request.prompt) > 32000:
        raise ModelError('invalid_image_prompt')
    allowed = {'n', 'size', 'quality', 'background', 'output_format', 'output_compression',
               'moderation', 'safety_identifier', 'response_format', 'style', 'user', 'timeout_seconds'}
    if not isinstance(request.options, dict) or set(request.options) - allowed:
        raise ModelError('invalid_image_options')
    options = {'quality': 'low', 'size': '1024x1024', **deepcopy(request.options)}
    timeout = options.pop('timeout_seconds', 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ModelError('invalid_image_options')
    enums = {
        'quality': {'auto', 'low', 'medium', 'high', 'xhigh', 'max', 'standard', 'hd'},
        'background': {'auto', 'opaque', 'transparent'},
        'output_format': {'png', 'jpeg', 'webp'},
        'moderation': {'auto', 'low'},
        'response_format': {'url', 'b64_json'},
        'style': {'vivid', 'natural'},
    }
    for key, values in enums.items():
        if key in options and (not isinstance(options[key], str) or options[key] not in values):
            raise ModelError('invalid_image_options')
    if 'n' in options and (type(options['n']) is not int or not 1 <= options['n'] <= 10):
        raise ModelError('invalid_image_options')
    size = options['size']
    if not isinstance(size, str) or (size != 'auto' and not re.fullmatch(r'[1-9][0-9]{0,5}x[1-9][0-9]{0,5}', size)):
        raise ModelError('invalid_image_options')
    if 'output_compression' in options:
        value = options['output_compression']
        if (type(value) is not int or not 0 <= value <= 100
                or options.get('output_format') not in ('jpeg', 'webp')):
            raise ModelError('invalid_image_options')
    if options.get('background') == 'transparent' and options.get('output_format') == 'jpeg':
        raise ModelError('invalid_image_options')
    for key in ('safety_identifier', 'user'):
        if key in options and (not isinstance(options[key], str) or not options[key].strip()):
            raise ModelError('invalid_image_options')
    return {'model': request.model, 'prompt': request.prompt, 'stream': False, **options}, float(timeout)


def parse_image_response(response, *, expected_count=1) -> ImageResult:
    if not isinstance(response, dict) or not isinstance(response.get('data'), list):
        raise ModelError('invalid_response')
    if not response['data']:
        raise ModelError('empty_image_result')
    if len(response['data']) > expected_count:
        raise ModelError('invalid_response')
    images = []
    for row in response['data']:
        if not isinstance(row, dict):raise ModelError('invalid_response')
        encoded, url, revised = row.get('b64_json'), row.get('url'), row.get('revised_prompt')
        if ((encoded is not None and (not isinstance(encoded, str) or not encoded))
                or (url is not None and (not isinstance(url, str) or not url))
                or (revised is not None and not isinstance(revised, str))):
            raise ModelError('invalid_response')
        if encoded is None and url is None:raise ModelError('invalid_response')
        data = None
        if encoded is not None:
            try:data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):raise ModelError('invalid_image_data') from None
            if not data:raise ModelError('invalid_image_data')
        images.append(GeneratedImage(data, url, revised))
    raw_usage = response.get('usage') or {}
    if not isinstance(raw_usage, dict):raise ModelError('invalid_response')
    usage = {}
    for old, new in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')):
        value = raw_usage.get(old)
        if type(value) is int and value >= 0:usage[new] = value
    for key in ('input_tokens_details', 'output_tokens_details'):
        details = raw_usage.get(key)
        if isinstance(details, dict):
            usage[key] = {name: value for name, value in details.items()
                          if name in ('text_tokens', 'image_tokens') and type(value) is int and value >= 0}
    metadata = {key: response.get(key) for key in ('output_format', 'size', 'quality', 'background')}
    if any(value is not None and not isinstance(value, str) for value in metadata.values()):
        raise ModelError('invalid_response')
    created = response.get('created')
    if created is not None and (type(created) is not int or created < 0):raise ModelError('invalid_response')
    return ImageResult(tuple(images), created, usage, **metadata)
