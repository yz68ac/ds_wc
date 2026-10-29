"""Application configuration and queue-facing native API records."""
from wechat_linux.native.protocol import (
    CHAT_ID, MAX_FRAME, REQUEST_ID, NativeError, decode, encode, receive, request,
    text_parts, validate_send,
)


def settings(cfg):
    data = cfg.get('native_api') or {}
    if not data:
        return {'enabled': False}
    required = {'enabled', 'socket', 'helper_socket', 'allowed_chat_ids', 'max_text_bytes', 'max_pending', 'max_requests'}
    if not isinstance(data, dict) or set(data) != required or type(data['enabled']) is not bool:
        raise NativeError('invalid_native_config')
    for key in ('socket', 'helper_socket'):
        if not isinstance(data[key], str) or not data[key].startswith('/') or len(data[key].encode()) > 100:
            raise NativeError('invalid_native_socket')
    if data['socket'] == data['helper_socket']:
        raise NativeError('invalid_native_socket')
    ids = data['allowed_chat_ids']
    if not isinstance(ids, list) or not ids or len(ids)>16 or any(not isinstance(x,str) or not CHAT_ID.fullmatch(x) for x in ids):
        raise NativeError('invalid_native_allowlist')
    for key, minimum, maximum in [('max_text_bytes',1,4096),('max_pending',1,32),('max_requests',1,4096)]:
        if type(data[key]) is not int or not minimum <= data[key] <= maximum:
            raise NativeError('invalid_native_limit')
    return dict(data)


def task_status(task):
    if task.get('external_error'):
        state = 'failed'
    elif task['phase'] == 'done':
        state = 'confirmed'
    elif task.get('blocked_reason'):
        state = 'blocked'
    elif task.get('intent'):
        state = 'confirming'
    else:
        state = 'queued'
    return dict(request_id=task['external_request_id'], task_id=task['id'], state=state,
                ack_ids=task.get('external_ack_ids', []), error=task.get('external_error') or
                ('send_confirmation_timeout' if task.get('blocked_reason') else ''),
                accepted_at=task['accepted_at'], completed_at=task.get('completed_at'))
