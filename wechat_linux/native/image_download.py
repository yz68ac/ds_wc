"""Unprivileged, explicit native image downloads. No Frida, database or UI access."""
from __future__ import annotations

import hashlib
import json
import math
import time

from wechat_linux.errors import ImageDownloadError, NativeError, MediaError
from wechat_linux.models import Image
from wechat_linux.media.images import read_local_image
from wechat_linux.native import image_contract as contract
from wechat_linux.native.protocol import request


class NativeImageDownloader:
    """Use an explicitly enabled, root-owned helper; never start or install one.

    A request ID is submitted at most once by this instance. Reuse only queries
    helper status. Neither IPC success nor native completion proves cache quality.
    """

    def __init__(self, helper_socket, allowed_chats, expected_account):
        if (not isinstance(helper_socket, str) or not helper_socket.startswith('/run/')
                or len(helper_socket.encode()) > 100 or '\0' in helper_socket
                or not isinstance(expected_account, str) or not contract.USER_ID.fullmatch(expected_account)):
            raise ValueError('invalid_native_image_options')
        chats = tuple(allowed_chats)
        if not chats or len(chats) > 16 or any(not isinstance(c, str) or not contract.CHAT_ID.fullmatch(c) for c in chats):
            raise ValueError('invalid_native_image_options')
        self.socket_path, self.allowed_chats, self.account = helper_socket, chats, expected_account
        self._requests = {}

    def _call(self, value, timeout):
        try:
            result = request(self.socket_path, value, timeout=timeout, peer_uid=0)
        except NativeError as exc:
            code = 'native_image_peer_rejected' if exc.code in ('native_peer_rejected', 'native_peer_unavailable') else 'native_image_service_unavailable'
            raise ImageDownloadError(code, requested=exc.submitted) from None
        if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                or result.get('requested') not in (None, True, False)
                or (result.get('requested') is not None and type(result['requested']) is not bool)):
            raise ImageDownloadError('native_image_invalid_response', requested=None)
        if not result['ok']:
            raise ImageDownloadError(result.get('error') if result.get('error') in contract.ERRORS else 'native_image_failed',
                                     requested=result.get('requested'))
        state = result.get('state')
        if state == 'pending':
            return {'state': state, 'requested': None}
        if (state != 'native_completed' or result.get('requested') is not True
                or result.get('source_verified') is not True or not contract.successful_status_pair(result.get('download_status'))):
            raise ImageDownloadError('native_image_invalid_response', requested=None)
        return {'state': state, 'requested': True}

    def get_status(self, request_id, *, timeout=5):
        if not contract.valid_id(request_id) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 35:
            raise ValueError('invalid_native_image_options')
        return self._call({'op': 'image_status', 'request_id': request_id}, timeout)

    def download(self, binding, *, chat_id, request_id, timeout=30, max_bytes=8*1024*1024):
        if (type(timeout) is not int or not 5 <= timeout <= 30 or type(max_bytes) is not int
                or not 1 <= max_bytes <= 8*1024*1024):
            raise ValueError('invalid_native_image_options')
        source = binding.get('download') if isinstance(binding, dict) else None
        if (not isinstance(source, dict) or source.get('verified') is not True
                or source.get('chat_id') != chat_id or binding.get('account') != self.account):
            raise ImageDownloadError('native_image_source_unverified', requested=False)
        value = dict(op='download_image', request_id=request_id, account=binding['account'], chat_id=chat_id,
                     server_id=source.get('server_id'), sender=source.get('sender'),
                     image_md5=source.get('image_md5'), timeout_seconds=timeout)
        try:
            contract.validate(value, self.allowed_chats, self.account)
        except ValueError as exc:
            raise ImageDownloadError(str(exc), requested=False) from None
        # Timeout is a wait budget, not part of the identity of the image operation.
        identity = {k: v for k, v in value.items() if k != 'timeout_seconds'}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if request_id in self._requests and self._requests[request_id] != digest:
            raise ImageDownloadError('native_image_request_conflict', requested=False)
        cfg = {'vision': {'max_image_bytes': max_bytes}}
        started = time.monotonic()
        deadline = started + timeout

        def cached():
            data, info = read_local_image(binding, cfg)
            if info['quality'] not in ('original', 'cached'):
                return None
            info = dict(info, download={'backend': 'native', 'outcome': 'cache_verified',
                                       'elapsed_ms': round((time.monotonic()-started)*1000)})
            return Image(data, info)

        try:
            found = cached()
        except MediaError:
            found = None
        if found is not None:
            return found
        if request_id not in self._requests:
            if len(self._requests) >= 4096:
                raise ImageDownloadError('native_image_history_full', requested=False)
            # Record intent BEFORE IPC. An uncertain transport must not be retried.
            self._requests[request_id] = digest
            result = self._call(value, max(.1, deadline-time.monotonic()))
        else:
            result = self.get_status(request_id, timeout=max(.1, deadline-time.monotonic()))
        while True:
            try:
                found = cached()
            except MediaError:
                found = None  # Partial writes may become a valid cache later.
            if found is not None:
                return found
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                code = 'native_image_cache_unverified' if result['state'] == 'native_completed' else 'native_image_timeout'
                raise ImageDownloadError(code, requested=result['requested'])
            time.sleep(min(.25, remaining))
            if result['state'] == 'pending':
                result = self.get_status(request_id, timeout=min(5, max(.1, deadline-time.monotonic())))
