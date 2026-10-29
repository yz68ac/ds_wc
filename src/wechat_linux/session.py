"""Explicit account access; callers own authorization and delivery scheduling."""
from __future__ import annotations

from dataclasses import dataclass
import math

from wechat_linux.errors import StoreError
from wechat_linux.models import Image, Message, MessageBatch
from wechat_linux.storage.store import WeChatStore


@dataclass(frozen=True)
class SessionConfig:
    db_dir: str
    keys_file: str
    decrypted_dir: str
    expected_account: str | None = None


class WeChatSession:
    def __init__(self, config: SessionConfig, *, sender=None, image_downloader=None):
        self.config = config
        self.sender = sender
        self.image_downloader = image_downloader
        self.store = None

    def __enter__(self):
        if self.store is not None:
            raise StoreError('session_already_open')
        try:
            self.store = WeChatStore(self.config.db_dir, self.config.keys_file, self.config.decrypted_dir)
            if not self.store.self_wxid:
                raise StoreError('account_not_ready')
            if self.config.expected_account and self.store.self_wxid != self.config.expected_account:
                raise StoreError('account_mismatch')
            return self
        except BaseException:
            self.close()
            raise

    def _chat(self, chat_id):
        if self.store is None:
            raise StoreError('session_not_open')
        if not isinstance(chat_id, str) or not chat_id or '\0' in chat_id:
            raise ValueError('invalid_chat_id')
        return chat_id

    @property
    def account_id(self):
        if self.store is None:
            raise StoreError('session_not_open')
        return self.store.self_wxid

    def read_messages(self, chat_id, *, after, limit=100):
        self._chat(chat_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid_receive_cursor')
        rows = self.store.messages_after(chat_id, after, limit=limit)
        return MessageBatch(
            tuple(Message.from_mapping(chat_id, message) for message in rows),
            max([after] + [message['local_id'] for message in rows]),
        )

    def fetch_image(self, chat_id, reference, *, download=False, timeout=20, request_id=None,
                    max_bytes=8*1024*1024):
        from wechat_linux.media.images import read_local_image
        if (type(download) is not bool or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 5 <= timeout <= 30):
            raise ValueError('invalid_image_options')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 8*1024*1024:
            raise ValueError('invalid_image_options')
        self._chat(chat_id)
        binding = self.store.image_binding(chat_id, reference)
        if download:
            if self.image_downloader is None:
                raise StoreError('image_downloader_not_configured')
            return self.image_downloader.download(
                binding, chat_id=chat_id, request_id=request_id, timeout=timeout, max_bytes=max_bytes,
            )
        data, info = read_local_image(binding, max_bytes=max_bytes)
        return Image(data, info)

    def send_text(self, chat_id, text, *, request_id):
        self._chat(chat_id)
        if self.sender is None:
            raise StoreError('sender_not_configured')
        return self.sender.submit_text(chat_id, text, request_id=request_id)

    def send_image(self, chat_id, image_path, *, request_id):
        self._chat(chat_id)
        if self.sender is None:
            raise StoreError('sender_not_configured')
        return self.sender.submit_image(chat_id, image_path, request_id=request_id)

    def close(self):
        # The injected sender and downloader remain owned by their caller.
        self.store = None

    def __exit__(self, *args):
        self.close()
