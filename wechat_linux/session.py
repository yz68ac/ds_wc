"""Explicit, exclusive account session for projects without the DeepSeek bot."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time

from wechat_linux.errors import StoreError
from wechat_linux.models import Image,Message,MessageBatch
from wechat_linux.runtime.locking import instance_lock
from wechat_linux.storage.store import WeChatStore


@dataclass(frozen=True)
class SessionConfig:
    db_dir: str
    keys_file: str
    decrypted_dir: str
    allowed_chats: tuple[str,...] = ()
    expected_account: str | None = None
    outbox_file: str | None = None


class WeChatSession:
    def __init__(self,config:SessionConfig,*,submitter=None):
        self.config,self.submitter=config,submitter
        self.store=self.outbox=self._lock=None

    def __enter__(self):
        if self._lock is not None:raise StoreError('session_already_open')
        self._lock=instance_lock(Path(self.config.decrypted_dir)/'.listener.lock')
        try:self._lock.__enter__()
        except BaseException:self._lock=None;raise
        try:
            self.store=WeChatStore(self.config.db_dir,self.config.keys_file,self.config.decrypted_dir)
            if not self.store.self_wxid:raise StoreError('account_not_ready')
            if self.config.expected_account and self.store.self_wxid!=self.config.expected_account:raise StoreError('account_mismatch')
            if self.submitter is not None:
                from wechat_linux.sending.outbox import ReliableSender
                if not self.config.outbox_file:raise StoreError('outbox_path_required')
                self.outbox=ReliableSender(self.config.outbox_file,self.store,self.submitter,self.config.allowed_chats)
            return self
        except BaseException:self.close();raise

    def _chat(self,chat_id):
        if self.store is None:raise StoreError('session_not_open')
        if chat_id not in self.config.allowed_chats:raise StoreError('chat_not_allowed')
        return chat_id

    @property
    def account_id(self):
        if self.store is None:raise StoreError('session_not_open')
        return self.store.self_wxid

    def read_messages(self,chat_id,*,after,limit=100):
        self._chat(chat_id)
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=200:raise ValueError('invalid_receive_cursor')
        rows=self.store.messages_after(chat_id,after,limit=limit)
        return MessageBatch(tuple(Message.from_mapping(chat_id,m) for m in rows),max([after]+[m['local_id'] for m in rows]))

    def fetch_image(self,chat_id,reference,*,download=False,timeout=20,ui=None,max_bytes=8*1024*1024):
        from wechat_linux.media.images import read_local_image
        if type(download) is not bool or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 5<=timeout<=30:
            raise ValueError('invalid_image_options')
        if type(max_bytes) is not int or not 1<=max_bytes<=8*1024*1024:raise ValueError('invalid_image_options')
        self._chat(chat_id);binding=self.store.image_binding(chat_id,reference)
        cfg=dict(vision=dict(max_image_bytes=max_bytes),queue=dict(file=str(Path(self.config.decrypted_dir).parent/'sdk-outbox.sqlite3')),
                 ui={'backend':'native_service',**(ui or {})})
        data,info=read_local_image(binding,cfg)
        if download and info['quality'] in ('thumbnail','preview'):
            from wechat_linux.desktop.image_download import improve_image
            if binding.get('download'):
                resolved,title=self.store.resolve_chat(chat_id)
                if resolved!=chat_id:raise StoreError('chat_mismatch')
                binding['download']['chat_title']=title
            data,info=improve_image(binding,cfg,data,info,time.monotonic()+timeout)
        return Image(data,info)

    def send_text(self,chat_id,text,*,request_id):
        self._chat(chat_id)
        if self.outbox is None:raise StoreError('sender_not_configured')
        return self.outbox.send_text(chat_id,text,request_id)

    def confirm(self,request_id):
        if self.outbox is None:raise StoreError('sender_not_configured')
        return self.outbox.confirm(request_id)

    def close(self):
        try:
            if self.outbox is not None:self.outbox.close();self.outbox=None
        finally:
            self.store=None
            if self._lock is not None:
                lock,self._lock=self._lock,None;lock.__exit__(None,None,None)

    def __exit__(self,*args):self.close()
