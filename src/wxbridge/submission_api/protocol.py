"""Application configuration and queue-facing submission API records."""
from wechat_linux.native.protocol import (
    CHAT_ID, MAX_FRAME, REQUEST_ID, NativeError, decode, encode, receive, request,
    text_parts, validate_send,
)


from dataclasses import dataclass
from wxbridge.config.models import SubmissionAPIConfig
from wxbridge.native.settings import settings as helper_settings


@dataclass(frozen=True)
class SubmissionSettings:
    enabled: bool
    socket: str
    max_pending: int
    max_requests: int
    allowed_chat_ids: tuple[str, ...]
    max_text_bytes: int


def settings(cfg):
    data = cfg.account.submission_api
    helper = helper_settings(cfg)
    if not isinstance(data, SubmissionAPIConfig) or data.extensions:
        raise NativeError('invalid_submission_config')
    if not data.socket.startswith('/') or len(data.socket.encode()) > 100:
        raise NativeError('invalid_native_socket')
    if data.enabled and data.socket == helper.socket:raise NativeError('invalid_native_socket')
    for key, minimum, maximum in [('max_pending',1,32),('max_requests',1,4096)]:
        if not minimum <= getattr(data,key) <= maximum:raise NativeError('invalid_native_limit')
    return SubmissionSettings(data.enabled,data.socket,data.max_pending,data.max_requests,
                              helper.allowed_chat_ids,helper.max_text_bytes)


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
