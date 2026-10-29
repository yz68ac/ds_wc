"""Bounded local framing, parameter checks, and metadata-only errors."""
from __future__ import annotations

import json
import os
import re
import socket
import struct

MAX_FRAME = 32768
MAX_IMAGE_BYTES = 8 * 1024 * 1024
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


def validate_send_image(value):
    fields = {'op', 'request_id', 'chat_id', 'image_path', 'image_bytes', 'image_sha256', 'image_mime'}
    if not isinstance(value, dict) or set(value) != fields or value['op'] != 'send_image':
        raise NativeError('invalid_request', submitted=False)
    if not isinstance(value['request_id'], str) or not REQUEST_ID.fullmatch(value['request_id']):
        raise NativeError('invalid_request_id', submitted=False)
    if not isinstance(value['chat_id'], str) or not CHAT_ID.fullmatch(value['chat_id']):
        raise NativeError('invalid_chat_id', submitted=False)
    path = value['image_path']
    try:
        valid_path = (isinstance(path, str) and path.startswith('/') and '\0' not in path
                      and len(path.encode('utf-8')) <= 4096 and os.path.normpath(path) == path)
    except UnicodeError:
        valid_path = False
    if not valid_path:
        raise NativeError('invalid_image_path', submitted=False)
    if type(value['image_bytes']) is not int or not 1 <= value['image_bytes'] <= MAX_IMAGE_BYTES:
        raise NativeError('image_size_limit', submitted=False)
    if not isinstance(value['image_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['image_sha256']):
        raise NativeError('invalid_image_digest', submitted=False)
    if value['image_mime'] not in ('image/png', 'image/jpeg'):
        raise NativeError('unsupported_image_format', submitted=False)
    return dict(value)


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


def request(path, value, timeout=5, *, peer_uid=None):
    may_have_submitted=False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(timeout)
            stream.connect(path)
            if peer_uid is not None:
                if not hasattr(socket, 'SO_PEERCRED'):
                    raise NativeError('native_peer_unavailable', submitted=False)
                credentials = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if credentials[1] != peer_uid:
                    raise NativeError('native_peer_rejected', submitted=False)
            may_have_submitted=True
            stream.sendall(encode(value))
            return receive(stream)
    except (OSError, TimeoutError):
        raise NativeError('native_service_unavailable',submitted=None if may_have_submitted else False) from None
