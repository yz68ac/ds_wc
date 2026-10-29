"""Bot deployment policy and message presentation around the WeChat adapter."""
from wechat_linux.errors import StoreError
from wechat_linux.storage.store import (
    WeChatStore as AdapterStore, discover_db_dir, format_time, parse_mentioned_users,
)
from wxbridge.storage.memoryfs import require_memory_directory
from wxbridge.wechat.messages import display_message


class WeChatStore(AdapterStore):
    def __init__(self, db_dir, keys_file, decrypted_dir):
        require_memory_directory(decrypted_dir)
        super().__init__(db_dir, keys_file, decrypted_dir)

    def recent_messages(self, chat_id, limit=20):
        return [display_message(message) for message in super().recent_messages(chat_id, limit)]

    def messages_in_window(self, chat_id, start, end):
        return [display_message(message) for message in super().messages_in_window(chat_id, start, end)]

    def messages_after(self, chat_id, after_local_id, limit=50):
        return [display_message(message) for message in super().messages_after(chat_id, after_local_id, limit)]
