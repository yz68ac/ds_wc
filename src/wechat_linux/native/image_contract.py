"""Image IPC contract, shared with the isolated helper; standard library only."""
from __future__ import annotations

import re

REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z')
CHAT_ID = re.compile(r'[A-Za-z0-9_-]{1,128}@chatroom\Z')
USER_ID = re.compile(r'(?:wxid_)?[A-Za-z0-9_-]{1,128}\Z')
MD5 = re.compile(r'[0-9a-f]{32}\Z')
FIELDS = {'op', 'request_id', 'account', 'chat_id', 'server_id', 'sender', 'image_md5', 'timeout_seconds'}
ASSETS = ('image_worker.py', 'image_contract.py', 'image_agent.js', 'image_adapter.c', 'image_profile.json')
ERRORS = frozenset({
    'native_image_disabled', 'native_image_invalid_request', 'native_image_chat_not_allowed',
    'native_image_account_mismatch', 'native_image_profile_mismatch', 'native_image_wrong_client',
    'native_image_dependency_missing', 'native_image_busy', 'native_image_request_conflict',
    'native_image_request_missing', 'native_image_history_full', 'native_image_timeout',
    'native_image_source_mismatch', 'native_image_not_found', 'native_image_failed',
    'native_image_cleanup_failed', 'native_image_service_unavailable', 'native_image_peer_rejected',
    'native_image_invalid_response', 'native_image_cache_unverified', 'native_image_source_unverified',
})


def valid_id(value):
    return isinstance(value, str) and REQUEST_ID.fullmatch(value) is not None


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS or value.get('op') != 'download_image':
        raise ValueError('native_image_invalid_request')
    if (not valid_id(value['request_id']) or not isinstance(value['account'], str)
            or not USER_ID.fullmatch(value['account']) or not isinstance(value['chat_id'], str)
            or not CHAT_ID.fullmatch(value['chat_id']) or not isinstance(value['sender'], str)
            or not USER_ID.fullmatch(value['sender']) or not isinstance(value['image_md5'], str)
            or not MD5.fullmatch(value['image_md5'])):
        raise ValueError('native_image_invalid_request')
    sid = value['server_id']
    if (not isinstance(sid, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', sid)
            or int(sid) > 2**64-1 or type(value['timeout_seconds']) is not int
            or not 5 <= value['timeout_seconds'] <= 30):
        raise ValueError('native_image_invalid_request')
    return dict(value)


def failure(code, requested=False):
    return {'ok': False, 'error': code if code in ERRORS else 'native_image_failed', 'requested': requested}


def safe_to_unload(status):
    """Never tear down a CModule while native code can still call it."""
    return (isinstance(status, dict) and status.get('pinned') is False
            and status.get('native_inflight') is False
            and type(status.get('callback_references')) is int and status['callback_references'] == 0)


def successful_status_pair(value):
    return isinstance(value, list) and len(value) == 2 and all(type(x) is int and x == 0 for x in value)


def completed(status):
    return (safe_to_unload(status) and status.get('stage') == 'done'
            and status.get('cleanup') is True and status.get('message_verified') is True
            and type(status.get('coroutine_completion_state')) is int and status['coroutine_completion_state'] == 1
            and status.get('download_call_returned') is True
            and type(status.get('download_future_state')) is int and status['download_future_state'] == 1
            and successful_status_pair(status.get('download_status'))
            and not status.get('error') and not status.get('unconfirmed'))
