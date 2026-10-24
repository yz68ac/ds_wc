"""Nonblocking Unix socket served only by the existing listener/main DB owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import socket
import stat
import struct
import time

from wxbridge.native.protocol import MAX_FRAME, REQUEST_ID, NativeError, decode, encode, settings, validate_send
from wxbridge.storage.memoryfs import require_memory_directory


class NativeAPI:
    def __init__(self, bot):
        self.bot, self.cfg = bot, settings(bot.cfg)
        self.clients, self.selector, self.listener, self.inode = {}, selectors.DefaultSelector(), None, None
        self.path = Path(self.cfg['socket'])
        require_memory_directory(str(self.path.parent))
        if self.path.is_symlink():
            raise NativeError('unsafe_native_socket')
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise NativeError('unsafe_native_socket')
            # The same listener instance lock is held before this class is constructed.
            self.path.unlink()
        try:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path)); self.path.chmod(0o600)
            self.inode = self.path.stat().st_ino
            self.listener.listen(8); self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise

    def handle(self, value):
        if value == {'op':'info'}:
            return dict(enabled=True, transport='native', allowed_chat_ids=self.cfg['allowed_chat_ids'],
                        max_text_bytes=self.cfg['max_text_bytes'])
        if isinstance(value, dict) and value.get('op') == 'status':
            identifier = value.get('request_id')
            if set(value) != {'op','request_id'} or not isinstance(identifier,str) or not REQUEST_ID.fullmatch(identifier):
                raise NativeError('invalid_request_id')
            return self.bot.journal.external_status(identifier)
        value = validate_send(value, self.cfg['max_text_bytes'])
        if value['chat_id'] not in self.cfg['allowed_chat_ids']:
            raise NativeError('chat_not_allowed')
        # Duplicate requests are resolved before any fresh DB/UI work or pending limit checks.
        duplicate = self.bot.journal.external_duplicate(value)
        if duplicate is not None:
            return duplicate
        identifier = value['chat_id']
        if identifier == 'filehelper':
            if identifier not in self.bot.store.contacts():
                raise NativeError('chat_not_found')
            title = 'File Transfer'
        elif identifier == self.bot.chat_id:
            title = self.bot.chat_title
        else:
            resolved, title = self.bot.store.resolve_chat(identifier)
            if resolved != identifier:
                raise NativeError('chat_not_found')
        # Make sure this target has readable local history before accepting a task.
        # This first trial deliberately requires a known existing message table.
        self.bot.store.last_local_id(identifier)
        return self.bot.journal.enqueue_external(value, title, time.time(),
            max_pending=self.cfg['max_pending'], max_requests=self.cfg['max_requests'])

    def _drop(self, client):
        self.clients.pop(client, None)
        try:self.selector.unregister(client)
        except (KeyError, ValueError):pass
        client.close()

    def tick(self):
        now = time.monotonic()
        for client, state in list(self.clients.items()):
            if now >= state['deadline']:self._drop(client)
        for key, mask in self.selector.select(0)[:16]:
            stream = key.fileobj
            if stream is self.listener:
                for _ in range(8):
                    try:client,_ = self.listener.accept()
                    except BlockingIOError:break
                    peer = struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
                    if peer not in (0,os.geteuid()) or len(self.clients)>=8:
                        client.close();continue
                    client.setblocking(False)
                    self.clients[client] = dict(buffer=bytearray(), output=None, deadline=now+5)
                    self.selector.register(client,selectors.EVENT_READ)
                continue
            state = self.clients[stream]
            try:
                if mask & selectors.EVENT_WRITE:
                    sent=stream.send(state['output']);state['output']=state['output'][sent:]
                    if not state['output']:self._drop(stream)
                    continue
                part=stream.recv(MAX_FRAME+5-len(state['buffer']))
                if not part:self._drop(stream);continue
                state['buffer'].extend(part)
                if len(state['buffer'])<4:continue
                length=struct.unpack('!I',state['buffer'][:4])[0]
                if not 0<length<=MAX_FRAME:raise NativeError('frame_too_large')
                if len(state['buffer'])<length+4:continue
                if len(state['buffer'])!=length+4:raise NativeError('invalid_frame')
                try:
                    result=dict(ok=True,result=self.handle(decode(state['buffer'][4:])))
                except NativeError as exc:result=dict(ok=False,error=exc.code)
                except Exception:result=dict(ok=False,error='native_request_failed')
                state['output']=encode(result)
                self.selector.modify(stream,selectors.EVENT_WRITE)
            except BlockingIOError:continue
            except (OSError,ValueError):self._drop(stream)

    def close(self):
        for client in list(self.clients):self._drop(client)
        if self.listener:
            self.listener.close();self.listener=None
        self.selector.close()
        try:
            if self.inode is not None and self.path.lstat().st_ino == self.inode:self.path.unlink()
        except FileNotFoundError:pass
