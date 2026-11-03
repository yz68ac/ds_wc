"""Validate helper transport policy without enabling an external API."""
from wechat_linux.native.protocol import CHAT_ID, NativeError
from wxbridge.config.models import HelperTransportConfig


def settings(cfg):
    data = cfg.account.helper_transport
    if not isinstance(data, HelperTransportConfig) or data.extensions:
        raise NativeError('invalid_helper_transport')
    if not data.socket.startswith('/') or len(data.socket.encode()) > 100:
        raise NativeError('invalid_native_socket')
    if not data.allowed_chat_ids or len(data.allowed_chat_ids)>16 or any(not CHAT_ID.fullmatch(x) for x in data.allowed_chat_ids):
        raise NativeError('invalid_native_allowlist')
    if not 1 <= data.max_text_bytes <= 4096:raise NativeError('invalid_native_limit')
    return data
