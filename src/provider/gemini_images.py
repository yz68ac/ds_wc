"""Gemini native image generation through the official, caller-owned Gen AI SDK."""
import base64
import binascii
from copy import deepcopy
import math

from .adapters import ModelError
from .images import GeneratedImage, ImageResult

MAX_IMAGE_BYTES = 16 * 1024 * 1024


def build_gemini_image_payload(request):
    if not isinstance(request.model, str) or not request.model.strip():
        raise ModelError('missing_model')
    if not isinstance(request.prompt, str) or not 1 <= len(request.prompt.strip()) <= 32000:
        raise ModelError('invalid_image_prompt')
    options = deepcopy(request.options)
    if not isinstance(options, dict) or set(options) - {'aspect_ratio', 'image_size', 'timeout_seconds'}:
        raise ModelError('invalid_image_options')
    timeout = options.pop('timeout_seconds', 180)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ModelError('invalid_image_options')
    ratio = options.get('aspect_ratio', '1:1')
    if ratio not in ('1:1', '1:4', '1:8', '2:3', '3:2', '3:4', '4:1', '4:3', '4:5', '5:4', '8:1', '9:16', '16:9', '21:9'):
        raise ModelError('invalid_image_options')
    image = {'aspect_ratio': ratio}
    if 'image_size' in options:
        if options['image_size'] not in ('512', '1K', '2K', '4K'):
            raise ModelError('invalid_image_options')
        image['image_size'] = options['image_size']
    return dict(model=request.model, contents=request.prompt,
                config=dict(response_modalities=['TEXT', 'IMAGE'], image_config=image)), float(timeout)


def send_gemini_images(client, payload, timeout):
    from google.genai import errors
    request = deepcopy(payload)
    request['config']['http_options'] = dict(timeout=max(1, int(timeout * 1000)))
    try:
        return client.models.generate_content(**request)
    except errors.APIError as exc:
        status = exc.code
        if type(status) is int:
            raise ModelError('http_'+str(status), retryable=status in (408, 429) or status >= 500,
                             status=status) from None
        raise ModelError('image_generation_failed') from None
    except Exception as exc:
        import httpx
        if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
            raise ModelError('connection_failed', retryable=True) from None
        raise ModelError('invalid_response') from None


def _field(value, name, default=None):
    if isinstance(value, dict):
        camel = name.split('_')[0]+''.join(part.title() for part in name.split('_')[1:])
        return value.get(name, value.get(camel, default))
    return getattr(value, name, default)


def parse_gemini_image_response(response):
    candidates = _field(response, 'candidates', []) or []
    if not isinstance(candidates, (list, tuple)):
        raise ModelError('invalid_response')
    if not candidates:
        feedback = _field(response, 'prompt_feedback')
        raise ModelError('image_generation_blocked' if _field(feedback, 'block_reason') else 'empty_image_result')
    parts = _field(_field(candidates[0], 'content'), 'parts', []) or []
    if not isinstance(parts, (list, tuple)):
        raise ModelError('invalid_response')
    images, texts = [], []
    for part in parts:
        if _field(part, 'thought', False):
            continue
        text = _field(part, 'text')
        if isinstance(text, str):
            texts.append(text)
        blob = _field(part, 'inline_data')
        if blob is None:
            continue
        mime, data = _field(blob, 'mime_type'), _field(blob, 'data')
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            raise ModelError('unsupported_image_format')
        if isinstance(data, str):
            if len(data) > (MAX_IMAGE_BYTES+2)//3*4:
                raise ModelError('image_size_limit')
            try:
                data = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise ModelError('invalid_image_data') from None
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_IMAGE_BYTES:
            raise ModelError('invalid_image_data')
        images.append(GeneratedImage(data=data, mime=mime))
        if len(images) > 10:
            raise ModelError('invalid_response')
    if not images:
        raise ModelError('empty_image_result')
    raw = _field(response, 'usage_metadata')
    usage = {}
    for old, new in (('prompt_token_count', 'prompt_tokens'), ('candidates_token_count', 'completion_tokens'),
                     ('total_token_count', 'total_tokens')):
        count = _field(raw, old)
        if type(count) is int and count >= 0:
            usage[new] = count
    return ImageResult(tuple(images), usage=usage, text='\n'.join(texts))
