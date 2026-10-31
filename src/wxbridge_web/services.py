"""Fixed local supervisor operations; HTTP never supplies commands or paths."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import uuid

from wxbridge_web.repository import ManagementError
from wxbridge_web.control import BOT, SESSION, HELPER, RECOVERY, WEB, COMPONENTS, DEFAULT_SOCKET, request

class ServiceManager:
    def __init__(self, *, allow_control=False, control_socket=DEFAULT_SOCKET):
        self.allow_control = allow_control and os.name == 'posix'
        self.control_socket = control_socket

    def status(self):
        try:
            if os.name != 'posix':raise OSError()
            values = request('status', self.control_socket)['components']
            return {name:values.get(name,dict(state='unavailable',pid=0)) for name in COMPONENTS}
        except (OSError, ValueError, KeyError, TypeError):
            return {name:dict(state='unavailable',pid=0) for name in COMPONENTS}

    def perform(self, action):
        if action not in {'start','stop','apply'}:
            raise ManagementError('invalid_service_action', '不支持的服务操作。')
        if not self.allow_control:
            raise ManagementError('service_control_disabled', '当前环境未开放服务控制。', 403)
        try:
            return request(action, self.control_socket)
        except (OSError, ValueError):
            raise ManagementError('supervisor_unavailable', '主控暂时不可用，请检查 wxbridge.service。', 503) from None


class Operations:
    def __init__(self, services, repository):
        self.services, self.repository = services, repository
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='web-service')
        self.lock, self.items, self.running = threading.RLock(), [], False

    def submit(self, action):
        if action not in {'start','stop','apply'}:
            raise ManagementError('invalid_service_action', '不支持的服务操作。')
        if not self.services.allow_control:
            raise ManagementError('service_control_disabled', '当前环境未开放服务控制。', 403)
        if action != 'stop':
            self.repository.validate_start()
        with self.lock:
            if self.running:
                raise ManagementError('operation_busy', '已有服务操作正在执行，请等待完成。', 409)
            item = dict(id=uuid.uuid4().hex, action=action, state='running', started_at=time.time(), message='正在执行服务操作…')
            self.items = [item, *self.items[:19]]
            self.running = True
            self.executor.submit(self._run, item)
            return dict(item)

    def _run(self, item):
        try:
            result = self.services.perform(item['action'])
            with self.lock:
                item.update(state='done', **result)
        except ManagementError as exc:
            with self.lock:
                item.update(state='error', code=exc.code, message=exc.message)
        except Exception:
            with self.lock:
                item.update(state='error', code='service_failed', message='服务操作失败，请检查服务器状态。')
        finally:
            with self.lock:
                item['finished_at'], self.running = time.time(), False

    def snapshot(self):
        with self.lock:
            return [dict(item) for item in self.items]

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
