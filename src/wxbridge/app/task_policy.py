"""任务错误分类与重试策略；不访问数据库、API 或微信界面。"""
from dataclasses import dataclass
import re


NATIVE_TRANSIENT = frozenset({'native_service_unavailable', 'native_worker_busy',
    'native_wrong_client', 'native_services_unavailable', 'native_schedule_failed'})
UI_TRANSIENT = frozenset({'command_timeout', 'command_failed', 'activation_failed',
    'pointer_mismatch', 'pointer_unknown', 'button_obstructed'})


def submission_failure(metadata):
    """Only an explicit False proves a send was not submitted."""
    operation = metadata.get('operation') or {}
    definite = operation.get('submitted') is False
    code = metadata.get('error', 'send_failed')
    return definite, definite and code in NATIVE_TRANSIENT | UI_TRANSIENT


def participation_failure_code(result):
    code = result.get('error')
    known = {'participation_invalid_decision', 'participation_invalid_reply_target',
             'participation_stale_snapshot', 'participation_identity_mismatch',
             'participation_stale_result', 'participation_invalid_queue_state', 'participation_expired',
             'output_limit', 'connection_failed', 'deadline_exceeded', 'missing_api_key',
             'invalid_response', 'request_too_large', 'response_too_large', 'WorkerTimeout', 'WorkerExited',
             'provider_failed', 'incomplete_response', 'unsupported_response_item', 'unsupported_native_search',
             'invalid_native_search_result', 'invalid_tool_result', 'continuation_provider_mismatch',
             'ValueError', 'JSONDecodeError', 'ContextError'}
    if isinstance(code, str) and (code in known or re.fullmatch(r'http_[0-9]{3}', code)):
        return code
    return 'participation_worker_failed'


def memory_failure_code(result):
    code = result.get('error')
    known = {'output_limit': 'memory_output_limit', 'JSONDecodeError': 'memory_invalid_json',
             'WorkerTimeout': 'memory_worker_timeout', 'WorkerExited': 'memory_worker_exited',
             'connection_failed': 'memory_connection_failed', 'deadline_exceeded': 'memory_deadline_exceeded',
             'missing_api_key': 'memory_missing_api_key', 'invalid_response': 'memory_invalid_response'}
    known.update(incomplete_response='memory_incomplete_response', provider_failed='memory_provider_failed',
                 unsupported_response_item='memory_invalid_response')
    if isinstance(code, str) and re.fullmatch(r'http_[0-9]{3}', code):
        return 'memory_'+code
    return known.get(code, 'memory_worker_failed') if isinstance(code, str) else 'memory_worker_failed'


@dataclass(frozen=True)
class GenerationFailure:
    code: str
    retryable: bool

    @classmethod
    def from_result(cls, result):
        code = result.get('error', 'EmptyReply')
        # 工作进程只允许返回安全错误码，其他内容不进入状态或日志。
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', code):
            code = 'GenerationError'
        retryable = result.get('retryable')
        if type(retryable) is not bool:
            retryable = code in ('WorkerTimeout', 'WorkerExited', 'TimeoutError', 'ConnectionError', 'OSError')
        return cls(code, retryable)
