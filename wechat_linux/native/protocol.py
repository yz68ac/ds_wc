"""Bounded local framing, parameter checks, and metadata-only errors."""
from __future__ import annotations

import json
import re
import socket
import struct

MAX_FRAME = 32768
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z')
CHAT_ID = re.compile(r'(?:filehelper|wxid_[A-Za-z0-9_-]{1,120}|[A-Za-z0-9_-]{1,128}@chatroom)\Z')


from wechat_linux.errors import NativeError


def text_parts(text, limit=4096):
    """Split generated text at UTF-8 code-point boundaries without altering it."""
    parts, current, used = [], [], 0
    for unit in re.findall(r'\[[^\[\]\r\n]{1,20}\]|[\s\S]',text):
        size = len(unit.encode('utf-8'))
        if size > limit:
            raise NativeError('text_too_large')
        if used + size > limit:
            parts.append(''.join(current));current=[];used=0
        current.append(unit)
        used += size
    parts.append(''.join(current))
    if any(not part.strip() for part in parts):
        raise NativeError('invalid_text')
    return parts


def validate_send(value, max_bytes=4096):
    if not isinstance(value, dict) or set(value) != {'op', 'request_id', 'chat_id', 'text'} or value['op'] != 'send_text':
        raise NativeError('invalid_request')
    if not isinstance(value['request_id'], str) or not REQUEST_ID.fullmatch(value['request_id']):
        raise NativeError('invalid_request_id')
    if not isinstance(value['chat_id'], str) or not CHAT_ID.fullmatch(value['chat_id']):
        raise NativeError('invalid_chat_id')
    text = value['text']
    if not isinstance(text, str) or not text.strip() or '\0' in text:
        raise NativeError('invalid_text')
    try:
        length = len(text.encode('utf-8'))
    except UnicodeError:
        raise NativeError('invalid_text') from None
    if length > max_bytes:
        raise NativeError('text_too_large')
    return dict(value)


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


def encode(value):
    try:
        body = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    except (TypeError, ValueError):
        raise NativeError('invalid_frame') from None
    if not 0 < len(body) <= MAX_FRAME:
        raise NativeError('frame_too_large')
    return struct.pack('!I', len(body)) + body


def decode(body):
    try:
        value = json.loads(body.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise NativeError('invalid_frame') from None
    if not isinstance(value, dict):
        raise NativeError('invalid_frame')
    return value


def receive(stream):
    def exact(count):
        parts = bytearray()
        while len(parts) < count:
            part = stream.recv(count-len(parts))
            if not part:
                raise NativeError('connection_closed')
            parts.extend(part)
        return bytes(parts)
    count = struct.unpack('!I', exact(4))[0]
    if not 0 < count <= MAX_FRAME:
        raise NativeError('frame_too_large')
    return decode(exact(count))


def request(path, value, timeout=5):
    may_have_submitted=False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(timeout)
            stream.connect(path)
            may_have_submitted=True
            stream.sendall(encode(value))
            return receive(stream)
    except (OSError, TimeoutError):
        raise NativeError('native_service_unavailable',submitted=None if may_have_submitted else False) from None


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
