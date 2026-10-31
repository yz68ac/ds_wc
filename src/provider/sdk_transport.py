"""SDK transport for model providers; protocol adapters remain provider-neutral."""
from .adapters import ModelError


def create_sdk_client(protocol, base_url, api_key, settings, timeout):
    if protocol == 'gemini':
        from .gemini_transport import create_gemini_client
        return create_gemini_client(base_url, api_key, proxy_url=settings.get('proxy_url'))
    try:
        if protocol in ('anthropic', 'anthropic_messages'):
            import anthropic as sdk
            options = {'auth_token' if settings.get('anthropic_auth') == 'bearer' else 'api_key': api_key}
            headers = {}
            if settings.get('anthropic_version'):
                headers['anthropic-version'] = settings['anthropic_version']
            # SDK defaults own versioning/auth. Legacy gateways may explicitly request x-api-key.
            if settings.get('anthropic_auth') == 'api_key':
                headers.update({'Authorization': sdk.omit, 'x-api-key': api_key})
            if headers:
                options['default_headers'] = headers
            factory = sdk.Anthropic
        else:
            import openai as sdk
            options = {'api_key': api_key}
            factory = sdk.OpenAI
    except ImportError:
        raise ModelError('sdk_not_installed') from None
    # Anthropic SDK adds /v1; the bridge configuration includes it.
    sdk_base = base_url[:-3] if protocol in ('anthropic', 'anthropic_messages') and base_url.endswith('/v1') else base_url
    options.update(base_url=sdk_base, timeout=timeout, max_retries=0)
    if settings.get('proxy_url'):
        options['http_client'] = sdk.DefaultHttpxClient(proxy=settings['proxy_url'])
    return sdk, factory(**options)


def map_sdk_error(sdk, exc):
    if getattr(sdk, '__name__', '') == 'google.genai':
        from .gemini_transport import map_gemini_error
        return map_gemini_error(exc)
    if isinstance(exc, sdk.APIResponseValidationError):
        return ModelError('invalid_response')
    if isinstance(exc, sdk.APIStatusError):
        status = exc.status_code
        return ModelError('http_'+str(status), retryable=status in (408, 429) or status >= 500, status=status)
    if isinstance(exc, sdk.APIConnectionError):
        return ModelError('connection_failed', retryable=True)
    return None


def _call_sdk(sdk, operation, parameters, timeout):
    try:
        response = operation(**parameters, timeout=timeout)
        return response.to_dict(mode='json', warnings=False)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise ModelError('invalid_response') from None
    except Exception as exc:
        mapped = map_sdk_error(sdk, exc)
        if mapped is not None:
            raise mapped from None
        raise


def send(sdk, client, protocol, payload, timeout):
    """Use the caller's SDK instance and its normal response parsing."""
    if protocol == 'gemini':
        from .gemini_transport import send_gemini_content
        return send_gemini_content(client, payload, timeout)
    if protocol in ('anthropic', 'anthropic_messages'):
        required = {k: payload[k] for k in ('model', 'messages', 'max_tokens')}
        operation = client.messages.create
    elif protocol == 'openai_responses':
        required = {k: payload[k] for k in ('model', 'input')}
        operation = client.responses.create
    else:
        required = {k: payload[k] for k in ('model', 'messages')}
        operation = client.chat.completions.create
    extra = {k: v for k, v in payload.items() if k not in required}
    return _call_sdk(sdk, operation, {**required, 'extra_body': extra}, timeout)


def send_images(sdk, client, payload, timeout):
    return _call_sdk(sdk, client.images.generate, payload, timeout)
