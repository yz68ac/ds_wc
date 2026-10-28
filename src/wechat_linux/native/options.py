"""Native helper client options independent of any bot or queue configuration."""
from dataclasses import dataclass

from wechat_linux.native.protocol import CHAT_ID, NativeError, settings


@dataclass(frozen=True)
class NativeOptions:
    helper_socket: str
    allowed_chats: tuple[str, ...]
    max_text_bytes: int = 4096
    enabled: bool = True
    dry_run: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'allowed_chats', tuple(self.allowed_chats))
        if type(self.enabled) is not bool or type(self.dry_run) is not bool:
            raise NativeError('invalid_native_config')
        if (not isinstance(self.helper_socket, str) or (self.enabled and not self.helper_socket.startswith('/'))
                or len(self.helper_socket.encode()) > 100):
            raise NativeError('invalid_native_socket')
        if ((self.enabled and not self.allowed_chats) or len(self.allowed_chats) > 16
                or any(not isinstance(x, str) or not CHAT_ID.fullmatch(x) for x in self.allowed_chats)):
            raise NativeError('invalid_native_allowlist')
        if type(self.max_text_bytes) is not int or not 1 <= self.max_text_bytes <= 4096:
            raise NativeError('invalid_native_limit')

    @classmethod
    def from_config(cls, cfg):
        """Compatibility boundary for the existing full application configuration."""
        if isinstance(cfg, cls):
            return cfg
        data = settings(cfg)
        return cls(data.get('helper_socket', ''), tuple(data.get('allowed_chat_ids', ())),
                   data.get('max_text_bytes', 4096), data['enabled'], cfg.get('ui', {}).get('dry_run', False))
