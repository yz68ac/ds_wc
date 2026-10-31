"""Local control protocol shared by the Web client and service supervisor."""
import json
import socket

BOT, SESSION, HELPER, RECOVERY, WEB = 'bot', 'wechat', 'sender', 'recovery', 'web'
COMPONENTS = (SESSION, HELPER, BOT, WEB, RECOVERY)
ACTIONS = frozenset(('status', 'start', 'stop', 'apply'))
MAX_FRAME = 8192
DEFAULT_SOCKET = '/run/wxbridge-control/control.sock'


def receive(stream):
    data = bytearray()
    while b'\n' not in data:
        block = stream.recv(min(2048, MAX_FRAME + 1 - len(data)))
        if not block:
            raise ValueError('control_incomplete_frame')
        data.extend(block)
        if len(data) > MAX_FRAME:
            raise ValueError('control_frame_too_large')
    line, extra = data.split(b'\n', 1)
    if extra:
        raise ValueError('control_extra_frame')
    return json.loads(line)


def request(action, path=DEFAULT_SOCKET):
    if action not in ACTIONS:
        raise ValueError('invalid_control_action')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(str(path))
        stream.sendall(json.dumps({'action': action}).encode() + b'\n')
        result = receive(stream)
    if not isinstance(result, dict) or set(result) != {'ok', 'result'} or result['ok'] is not True:
        raise ValueError('control_request_failed')
    return result['result']
