"""Local Python client; no Frida dependency and no database access."""
from __future__ import annotations

from wxbridge.native.protocol import NativeError, request, validate_send


class NativeClient:
    def __init__(self, socket_path='/run/wxbridge-trial/native-api.sock', *, timeout=5):
        self.socket_path, self.timeout = socket_path, timeout

    def _call(self, value):
        result = request(self.socket_path, value, self.timeout)
        if result.get('ok') is not True:
            raise NativeError(result.get('error', 'native_request_failed'))
        return result['result']

    def send_text(self, chat_id, text, request_id):
        return self._call(validate_send(dict(op='send_text', request_id=request_id, chat_id=chat_id, text=text)))

    def get_status(self, request_id):
        return self._call(dict(op='status', request_id=request_id))

    def info(self):
        return self._call(dict(op='info'))
