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

from wechat_linux.native.protocol import MAX_FRAME, REQUEST_ID, NativeError, decode, encode, settings, validate_send
from wechat_linux.runtime.memoryfs import require_memory_directory


class NativeServer:
    def __init__(self, config, service, *, directory_validator=None):
        self.cfg, self.service = settings({'native_api':config}), service
        if not self.cfg['enabled']:raise NativeError('native_api_disabled')
        self.clients, self.selector, self.listener, self.inode = {}, None, None, None
        self.path = Path(self.cfg['socket'])
        (directory_validator or require_memory_directory)(str(self.path.parent))
        if self.path.is_symlink():
            raise NativeError('unsafe_native_socket')
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise NativeError('unsafe_native_socket')
            # The same listener instance lock is held before this class is constructed.
            self.path.unlink()
        try:
            self.selector = selectors.DefaultSelector()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path)); self.path.chmod(0o600)
            self.inode = self.path.stat().st_ino
            self.listener.listen(8); self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise

    def handle(self,value):
        return self.service.handle(value)

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
        if self.selector is not None:self.selector.close();self.selector=None
        try:
            if self.inode is not None and self.path.lstat().st_ino == self.inode:self.path.unlink()
        except FileNotFoundError:pass
