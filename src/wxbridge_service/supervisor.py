"""One root supervisor, fixed child commands, and unprivileged application processes."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import time

from wxbridge_web.control import ACTIONS, BOT, COMPONENTS, DEFAULT_SOCKET, HELPER, RECOVERY, SESSION, WEB, receive

log = logging.getLogger('wxbridge.service')
GUI_ENV = frozenset(('DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
    'QT_ACCESSIBILITY', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON', 'GTK_MODULES', 'NO_AT_BRIDGE'))


def atomic_json(path, value):
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.service-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Child:
    def __init__(self, name, command, *, uid=None, gid=None, groups=(), cwd=None, timeout=15):
        self.name, self.command = name, command
        self.uid, self.gid, self.groups, self.cwd = uid, gid, groups, cwd
        self.timeout, self.process, self.stopping, self.deadline = timeout, None, False, 0
        self.next_start, self.failures, self.started = 0, 0, 0

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, env, output=None, *, capture=False):
        if self.process is not None or time.monotonic() < self.next_start:
            return False
        args = dict(cwd=self.cwd, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
                    stdout=subprocess.PIPE if capture else output, stderr=subprocess.DEVNULL if capture else subprocess.STDOUT)
        if self.uid is not None:
            args.update(user=self.uid, group=self.gid, extra_groups=self.groups)
        self.process = subprocess.Popen(self.command, **args)
        self.stopping = False
        self.started = time.monotonic()
        log.info('component_started name=%s pid=%s', self.name, self.process.pid)
        return True

    def stop(self, sig=signal.SIGTERM):
        if self.process is not None and not self.stopping:
            self.stopping, self.deadline = True, time.monotonic() + self.timeout if self.timeout is not None else float('inf')
            self._signal(sig)

    def _signal(self, sig):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass

    def reap(self):
        if self.process is None:
            return None
        if self.stopping and self.running and time.monotonic() >= self.deadline:
            log.error('component_stop_timeout name=%s', self.name)
            self._signal(signal.SIGKILL)
        code = self.process.poll()
        if code is None:
            return None
        output = self.process.communicate()[0] if self.process.stdout else None
        intentional = self.stopping
        # The process group can still contain UI children after its leader exits.
        self._signal(signal.SIGTERM)
        self.process = None
        self.stopping = False
        if not intentional:
            self.failures = self.failures + 1 if time.monotonic() - self.started < 60 else 0
            self.next_start = time.monotonic() + min(30, 2 ** min(self.failures, 5))
        else:
            self.next_start = time.monotonic()
        log.info('component_exited name=%s code=%s intentional=%s', self.name, code, intentional)
        return code, output, intentional

    def status(self):
        return dict(state='deactivating' if self.running and self.stopping else 'active' if self.running else 'inactive',
                    pid=self.process.pid if self.running else 0)


class Supervisor:
    def __init__(self, root, *, web_host='127.0.0.1', web_port=8730,
                 control_path=DEFAULT_SOCKET, state_dir='/var/lib/wxbridge-service', run_dir='/run/wxbridge-trial'):
        import pwd
        self.root, self.run_dir = Path(root), Path(run_dir)
        self.account = pwd.getpwnam('wxbridge')
        self.state_dir, self.control_path = Path(state_dir), Path(control_path)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink() or self.state_dir.stat().st_uid != 0 or self.state_dir.stat().st_mode & 0o077:
            raise ValueError('unsafe_service_state_directory')
        self.state_file = self.state_dir / 'state.json'
        models = Path('/var/lib/wxbridge-web')
        if models.is_symlink():
            raise ValueError('unsafe_model_catalog_directory')
        models.mkdir(mode=0o700, exist_ok=True)
        if models.stat().st_uid not in (0, self.account.pw_uid):
            raise ValueError('unsafe_model_catalog_owner')
        os.chown(models, self.account.pw_uid, self.account.pw_gid)
        os.chmod(models, 0o700)
        if self.state_file.is_symlink():
            raise ValueError('unsafe_service_state_file')
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            if set(state) != {'schema', 'bot_enabled'} or state['schema'] != 1 or type(state['bot_enabled']) is not bool:
                raise ValueError('invalid_service_state')
            self.enabled = state['bot_enabled']
        else:
            self.enabled = any(json.loads(path.read_text(encoding='utf-8-sig')).get('enabled', True)
                               for path in (self.root/'instance').glob('*/config.json'))
            self.persist()
        self.stopping, self.ready, self.checkpoint_needed = False, False, False
        self.phase, self.error, self.next_prepare = 'starting', '', 0
        python = str(self.root/'.venv/bin/python')
        identity = dict(uid=self.account.pw_uid, gid=self.account.pw_gid,
                        groups=os.getgrouplist(self.account.pw_name, self.account.pw_gid), cwd=str(self.root))
        helper = Path('/opt/wxbridge-native-helper')
        self.children = {
            SESSION: Child(SESSION, ['/usr/bin/python3', str(self.root/'scripts/boot-session.py')], **identity),
            HELPER: Child(HELPER, [str(helper/'venv/bin/python'), '-I', str(helper/'helper.py'), '--serve', str(helper/'helper-config.json')], cwd=str(helper), timeout=None),
            BOT: Child(BOT, [python, str(self.root/'scripts/run-live.py')], timeout=330, **identity),
            WEB: Child(WEB, [python, str(self.root/'scripts/web-console.py'), '--root', str(self.root),
                '--host', web_host, '--port', str(web_port), '--password-file', '/etc/wxbridge/web-password',
                '--allow-service-control', '--control-socket', str(self.control_path),
                '--catalog-file', '/var/lib/wxbridge-web/models.json'], **identity),
            RECOVERY: Child(RECOVERY, [python, str(self.root/'scripts/boot-recovery.py'), 'prepare'], cwd=str(self.root), timeout=15),
            'checkpoint': Child('checkpoint', [python, str(self.root/'scripts/boot-recovery.py'), 'checkpoint'], cwd=str(self.root), timeout=15),
        }
        self.socket, self.logs, self.lock_fd = None, {}, None

    def persist(self):
        atomic_json(self.state_file, dict(schema=1, bot_enabled=self.enabled))

    def environment(self, *, gui=False):
        env = dict(PATH='/usr/local/bin:/usr/bin:/bin', HOME=self.account.pw_dir,
                   PYTHONPATH=str(self.root/'src'), WX_RUNTIME_DIR=str(self.run_dir), LANG='C.UTF-8')
        if gui and (self.run_dir/'env').is_file():
            for line in (self.run_dir/'env').read_text().splitlines():
                key, sep, value = line.partition('=')
                if sep and key in GUI_ENV:
                    env[key] = value.strip().strip('\'"')
        return env

    def output(self, name):
        if name not in self.logs:
            filename = {BOT: 'bot-live.log', SESSION: 'session-supervisor.log'}.get(name, name+'.log')
            path = self.run_dir / filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            os.fchown(fd, self.account.pw_uid, self.account.pw_gid)
            self.logs[name] = os.fdopen(fd, 'ab', buffering=0)
        return self.logs[name]

    def start(self, name, *, capture=False):
        child = self.children[name]
        try:
            return child.start(self.environment(gui=name != SESSION), None if capture else self.output(name), capture=capture)
        except (OSError, ValueError) as exc:
            child.next_start = time.monotonic() + 15
            self.error = 'component_start_' + type(exc).__name__
            log.error('component_start_failed name=%s category=%s', name, type(exc).__name__)
            return False

    def status(self):
        components = {name: self.children[name].status() for name in COMPONENTS}
        components[RECOVERY].update(state='active' if self.enabled and not self.stopping else 'inactive',
                                    substate=self.phase, enabled=self.enabled, error=self.error)
        return dict(components=components, bot_enabled=self.enabled, phase=self.phase, error=self.error)

    def action(self, action):
        if action not in ACTIONS:
            raise ValueError('invalid_control_action')
        if action == 'status':
            return self.status()
        if self.stopping:
            raise ValueError('service_stopping')
        bot = self.children[BOT]
        if action == 'apply' and not bot.running:
            return dict(applied=False, message='机器人处于停止状态；配置将在下次启动时加载。')
        previous = self.enabled
        self.enabled = action != 'stop'
        try:
            self.persist()
        except OSError:
            self.enabled = previous
            raise
        self.ready, self.next_prepare = False, 0
        if action in ('stop', 'apply'):
            bot.stop(signal.SIGINT)
            self.children[RECOVERY].stop()
        return dict(applied=True, message={'start': '启动请求已接收；微信就绪后自动运行机器人。',
                    'stop': '停止请求已接收；微信和管理台保持运行。', 'apply': '配置应用请求已接收；正在安全重启机器人。'}[action])

    def bind(self):
        import fcntl
        parent = self.control_path.parent
        if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise ValueError('unsafe_control_directory')
        self.lock_fd = os.open(parent/'supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.control_path.exists():
            if not stat.S_ISSOCK(self.control_path.lstat().st_mode):
                raise ValueError('unsafe_control_path')
            self.control_path.unlink()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.control_path))
        os.chown(self.control_path, 0, self.account.pw_gid)
        os.chmod(self.control_path, 0o660)
        self.socket.listen(8)
        self.socket.setblocking(False)

    def serve(self):
        for _ in range(8):
            try:
                stream, _ = self.socket.accept()
            except BlockingIOError:
                return
            with stream:
                try:
                    _, uid, _ = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid not in (0, self.account.pw_uid):
                        continue
                    stream.settimeout(1)
                    value = receive(stream)
                    if not isinstance(value, dict) or set(value) != {'action'} or not isinstance(value['action'], str):
                        raise ValueError('invalid_control_request')
                    result = dict(ok=True, result=self.action(value['action']))
                except (OSError, ValueError, TypeError):
                    result = dict(ok=False, result='control_request_failed')
                try:
                    stream.sendall(json.dumps(result).encode()+b'\n')
                except OSError:
                    pass

    def tick(self):
        for name, child in self.children.items():
            event = child.reap()
            if event is None:
                continue
            code, data, intentional = event
            if name == SESSION:
                self.ready = False
                for other in (BOT, HELPER, RECOVERY):
                    self.children[other].stop(signal.SIGINT if other == BOT else signal.SIGTERM)
            elif name == HELPER:
                self.ready = False
                self.children[BOT].stop(signal.SIGINT)
            elif name == BOT:
                self.ready, self.checkpoint_needed = False, True
            elif name == 'checkpoint':
                self.checkpoint_needed = False
                if code:
                    self.error = 'checkpoint_failed'
            elif name == RECOVERY:
                try:
                    value = json.loads((data or b'').decode())
                except (UnicodeError, ValueError):
                    value = {}
                if not intentional:
                    self.ready = code == 0 and value.get('ready') is True
                    self.error = '' if self.ready else 'waiting_for_login_or_repair'
                    self.next_prepare = time.monotonic() + (0 if self.ready else 30)
        bot = self.children[BOT]
        if not self.enabled or self.stopping:
            bot.stop(signal.SIGINT)
            self.children[RECOVERY].stop()
        if bot.process is None and self.checkpoint_needed and self.children['checkpoint'].process is None:
            self.start('checkpoint', capture=True)
        if self.stopping:
            if bot.process is None and not self.checkpoint_needed and self.children['checkpoint'].process is None:
                self.children[HELPER].stop()
                if self.children[HELPER].process is None:
                    self.children[SESSION].stop()
            return
        self.start(WEB)
        if self.children[SESSION].process is None and bot.process is None and self.children[HELPER].process is None:
            self.start(SESSION)
        desktop_ready = self.children[SESSION].running and (self.run_dir/'env').is_file() and (self.run_dir/'wechat.pid').is_file()
        if desktop_ready:
            self.start(HELPER)
        else:
            self.ready = False
        if self.enabled and desktop_ready and self.children[HELPER].running and bot.process is None and not self.checkpoint_needed and self.children['checkpoint'].process is None:
            if self.ready:
                self.start(BOT)
            elif self.children[RECOVERY].process is None and time.monotonic() >= self.next_prepare:
                self.start(RECOVERY, capture=True)
        self.phase = ('stopped' if not self.enabled else 'listening' if bot.running and not bot.stopping
                      else 'stopping' if bot.stopping else 'preparing' if self.children[RECOVERY].running
                      else 'waiting_for_login_or_repair')

    def run(self):
        self.bind()
        shutting_down = False
        orphan_deadline = None
        try:
            while True:
                self.serve()
                self.tick()
                if self.stopping:
                    if not shutting_down:
                        shutting_down = True
                        self.children[WEB].stop()
                    if all(child.process is None for child in self.children.values()):
                        # D-Bus and desktop portals can daemonize outside their
                        # parent's process group. Drain this unit's remaining
                        # processes only after the sender has finished safely.
                        remaining = self.remaining_processes()
                        if not remaining:
                            break
                        if orphan_deadline is None:
                            orphan_deadline = time.monotonic()+5
                        sig = signal.SIGTERM if time.monotonic()<orphan_deadline else signal.SIGKILL
                        for pid in remaining:
                            try:os.kill(pid,sig)
                            except ProcessLookupError:pass
                time.sleep(0.25)
        finally:
            for child in self.children.values():
                if child.process is not None:
                    child._signal(signal.SIGTERM if child.name == HELPER else signal.SIGKILL)
            if self.socket:
                self.socket.close()
                self.control_path.unlink(missing_ok=True)
            for stream in self.logs.values():
                stream.close()
            if self.lock_fd is not None:
                os.close(self.lock_fd)

    @staticmethod
    def remaining_processes():
        group = next((line.split(':',2)[2].strip() for line in Path('/proc/self/cgroup').read_text().splitlines()
                      if line.startswith('0::')), '')
        if not group.endswith('/wxbridge.service') or '..' in Path(group).parts:
            raise ValueError('supervisor_requires_own_systemd_cgroup')
        path = Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
        return [int(pid) for pid in path.read_text().split() if int(pid)!=os.getpid()]
