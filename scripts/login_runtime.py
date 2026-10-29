"""Application-owned login environment, process policy and private UI lock."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from wechat_linux.desktop.login import LoginController, LoginError, LoginStatus

GUI_ENVIRONMENT = frozenset({
    'DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
    'QT_ACCESSIBILITY', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON', 'GTK_MODULES', 'NO_AT_BRIDGE',
})


def private_file(path, *, maximum=32768):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > maximum):
        raise LoginError('login_runtime_permissions')
    return path.read_text(encoding='utf-8')


class LoginRuntime:
    def __init__(self, runtime_dir=None):
        self.directory = Path(runtime_dir or os.environ.get('WX_RUNTIME_DIR', '/run/wxbridge-trial')).absolute()
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise LoginError('login_runtime_permissions')
        for line in private_file(self.directory / 'env').splitlines():
            key, separator, value = line.partition('=')
            if separator and key in GUI_ENVIRONMENT:
                os.environ[key] = value.strip().strip('\'"')
        pid = int(private_file(self.directory / 'wechat.pid', maximum=32).strip())
        config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
        self.controller = LoginController(pid, config['db_dir'], ui_options=config.get('ui'))
        self.path = self.directory / 'login-state.json'

    @contextmanager
    def locked(self):
        import fcntl
        path = self.directory / 'login-ui.lock'
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise LoginError('login_runtime_permissions')
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LoginError('login_ui_busy') from None
                    time.sleep(0.1)
            yield
        finally:
            os.close(descriptor)

    def state(self):
        if not self.path.exists():
            return dict(process=self.controller.identity, attempted=[], completed=False, qr_refreshed=False)
        try:
            value = json.loads(private_file(self.path))
        except (ValueError, UnicodeError):
            raise LoginError('login_state_invalid') from None
        if (not isinstance(value, dict) or not isinstance(value.get('process'), dict)
                or set(value['process']) != {'pid', 'start_time', 'boot_id'}
                or type(value['process']['pid']) is not int or value['process']['pid'] <= 0
                or not all(isinstance(value['process'][key], str) and value['process'][key]
                           for key in ('start_time', 'boot_id'))
                or not isinstance(value.get('attempted'), list)
                or any(item not in ('login', 'open') for item in value['attempted'])
                or type(value.get('completed')) is not bool
                or type(value.get('qr_refreshed')) is not bool):
            raise LoginError('login_state_invalid')
        if value['process'] != self.controller.identity:
            return dict(process=self.controller.identity, attempted=[], completed=False, qr_refreshed=False)
        return value

    def save(self, state, status):
        state.update(phase=status.phase, reason=status.reason, updated_at=int(time.time()))
        if self.path.is_symlink():
            raise LoginError('login_runtime_permissions')
        descriptor, temporary = tempfile.mkstemp(prefix='.login-', dir=self.directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def advance(self):
        """Caller must hold locked(); reserve each action before dispatching it."""
        state = self.state()
        if state['completed']:
            return LoginStatus('completed', self.controller.pid)
        status = self.controller.inspect()
        if status.phase == 'logged_in':
            state['completed'] = True
        action = {'login_ready': 'login', 'open_ready': 'open'}.get(status.phase)
        if action and action not in state['attempted']:
            state['attempted'].append(action)
            self.save(state, LoginStatus('action_reserved', self.controller.pid))
            try:
                operation = self.controller.click_login if action == 'login' else self.controller.open_wechat
                operation()
            except LoginError as error:
                status = LoginStatus('waiting_manual', self.controller.pid, error.code)
            else:
                status = LoginStatus('phone_confirmation' if action == 'login' else 'opening_wechat',
                                     self.controller.pid)
        elif action:
            status = LoginStatus('phone_confirmation' if action == 'login' else 'waiting_manual',
                                 self.controller.pid, 'login_action_already_attempted')
        self.save(state, status)
        return status

    def viewer_snapshot(self):
        """Caller holds locked(); only the explicit viewer may refresh QR codes."""
        state = self.state()
        status = self.controller.inspect()
        if status.phase == 'qr_expired' and not state['qr_refreshed']:
            state['qr_refreshed'] = True
            self.save(state, status)
            try:
                self.controller.refresh_qr()
            except LoginError as error:
                status = LoginStatus('qr_expired', self.controller.pid, error.code)
            else:
                status = LoginStatus('waiting', self.controller.pid, 'login_qr_refresh_requested')
        elif status.phase == 'qr_required' and state['qr_refreshed']:
            state['qr_refreshed'] = False
            self.save(state, status)
        elif status.phase == 'login_ready' and 'login' in state['attempted']:
            status = LoginStatus('phone_confirmation', self.controller.pid,
                                 'login_action_already_attempted')
        elif status.phase == 'open_ready' and 'open' in state['attempted']:
            status = LoginStatus('waiting_manual', self.controller.pid,
                                 'login_action_already_attempted')
        return status


def background(runtime):
    previous = None
    while True:
        try:
            with runtime.locked():
                status = runtime.advance()
        except LoginError as error:
            status = LoginStatus('waiting', runtime.controller.pid, error.code)
        except Exception:
            status = LoginStatus('waiting', runtime.controller.pid, 'login_internal_error')
        signature = (status.phase, status.reason)
        if signature != previous:
            print(json.dumps(dict(phase=status.phase, reason=status.reason, pid=status.pid)), flush=True)
            previous = signature
        if status.phase in ('logged_in', 'completed', 'client_stopped'):
            return 1 if status.phase == 'client_stopped' else 0
        time.sleep(5)
