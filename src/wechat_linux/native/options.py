"""Connection and wire limits for the native helper client."""
from dataclasses import dataclass
import math

from wechat_linux.errors import NativeError


@dataclass(frozen=True)
class NativeOptions:
    helper_socket: str
    max_text_bytes: int = 4096
    timeout_seconds: float = 22

    def __post_init__(self):
        if (not isinstance(self.helper_socket, str) or not self.helper_socket.startswith('/')
                or chr(0) in self.helper_socket or len(self.helper_socket.encode()) > 100):
            raise NativeError('invalid_native_socket')
        if type(self.max_text_bytes) is not int or not 1 <= self.max_text_bytes <= 4096:
            raise NativeError('invalid_native_limit')
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise NativeError('invalid_native_timeout')
