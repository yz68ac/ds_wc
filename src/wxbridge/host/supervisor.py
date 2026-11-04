"""One root supervisor, fixed child commands, and unprivileged application processes."""
from __future__ import annotations

import json
from http.client import HTTPException
import logging
import os
from pathlib import Path
import signal
import secrets
import socket
import stat
import struct
import subprocess
import time
import threading
import urllib.request

from wxbridge.control import ACTIONS, WEB_ACTIONS, BOT, COMPONENTS, DEFAULT_SOCKET, HELPER, RECOVERY, SESSION, WEB, receive
from .settings import WEB_CONFIG, WEB_PASSWORD, DEFAULT_WEB, read_web, migrate_state, web_document

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
    container = False
    forced_shutdown = False
    def __init__(self, root, *, web_config=WEB_CONFIG,
                 control_path=DEFAULT_SOCKET, state_dir='/var/lib/wxbridge-service', run_dir='/run/wxbridge-trial'):
        import pwd
        from .layout import container, code_root
        self.container = container()
        self.code = code_root() if self.container else Path(root)
        self.root, self.run_dir = Path(root), Path(run_dir)
        self.helper_stop_started = None
        self.wechat_stop_started = None
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
            state = migrate_state(json.loads(self.state_file.read_text()))
            self.enabled = state['bot_enabled']
            self.web_enabled = state['web_enabled']
        else:
            self.enabled = any(json.loads(path.read_text(encoding='utf-8-sig')).get('enabled', True)
                               for path in (self.root/'instance').glob('*/config.json'))
            self.web_enabled = True
        self.persist()
        self.web_config = Path(web_config)
        self.web_error = ''
        try:
            self.web_saved = read_web(self.web_config)
        except (OSError, ValueError):
            self.web_saved, self.web_error = None, 'web_settings_invalid'
        self.web_effective = None
        self.web_generation, self.web_applied_generation = 0, -1
        self.web_ready, self.web_next_probe = False, 0
        self.web_health_token = ''
        self.web_launch_settings = None
        self.stopping, self.ready, self.checkpoint_needed = False, False, False
        self.phase, self.error, self.next_prepare = 'starting', '', 0
        python = str(self.code/'.venv/bin/python')
        identity = dict(uid=self.account.pw_uid, gid=self.account.pw_gid,
                        groups=os.getgrouplist(self.account.pw_name, self.account.pw_gid), cwd=str(self.root))
        helper = Path('/opt/wxbridge-native-helper')
        self.children = {
            SESSION: Child(SESSION, ['/usr/bin/python3', str(self.code/'scripts/boot-session.py')], **identity),
            HELPER: Child(HELPER, [str(helper/'venv/bin/python'), '-I', str(helper/'helper.py'), '--serve', str(Path('/etc/wxbridge/helper-config.json') if self.container else helper/'helper-config.json')], cwd=str(helper), timeout=None),
            BOT: Child(BOT, [python, '-m', 'wxbridge.app.runtime', '--config', str(self.root/'config.json')], timeout=330, **identity),
            WEB: Child(WEB, [python, '-m', 'web.backend', '--root', str(self.root),
                '--host', DEFAULT_WEB.host, '--port', str(DEFAULT_WEB.port), '--password-file', str(WEB_PASSWORD),
                '--allow-service-control', '--control-socket', str(self.control_path),
                '--catalog-file', '/var/lib/wxbridge-web/models.json'], **identity),
            RECOVERY: Child(RECOVERY, [python, str(self.code/'scripts/boot-recovery.py'), 'prepare'], cwd=str(self.root), timeout=15),
            'checkpoint': Child('checkpoint', [python, str(self.code/'scripts/boot-recovery.py'), 'checkpoint'], cwd=str(self.root), timeout=15),
        }
        self.socket, self.logs, self.lock_fd = None, {}, None
        self.log_threads = []

    def persist(self):
        atomic_json(self.state_file, dict(schema=2, bot_enabled=self.enabled, web_enabled=self.web_enabled))

    def environment(self, *, gui=False):
        from .layout import environment
        env = dict(**environment(), PATH='/usr/local/bin:/usr/bin:/bin', HOME=self.account.pw_dir,
                   WX_RUNTIME_DIR=str(self.run_dir), LANG='C.UTF-8')
        if self.container:
            env['PYTHONPATH'] = str(self.code/'src')
            env['WXBRIDGE_WEB_HOSTS'] = os.environ.get('WXBRIDGE_WEB_HOSTS', '')
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
            if self.container:
                reader, writer = os.pipe()
                def relay(source=reader, destination=fd):
                    try:
                        while True:
                            chunk = os.read(source, 65536)
                            if not chunk:
                                break
                            if os.fstat(destination).st_size > 8*1024*1024:
                                os.ftruncate(destination, 0)
                            os.write(destination, chunk)
                            os.write(1, chunk)
                    finally:
                        os.close(source)
                        os.close(destination)
                thread = threading.Thread(target=relay, daemon=True)
                thread.start()
                self.log_threads.append(thread)
                self.logs[name] = os.fdopen(writer, 'wb', buffering=0)
            else:
                self.logs[name] = os.fdopen(fd, 'ab', buffering=0)
        return self.logs[name]

    def start(self, name, *, capture=False):
        child = self.children[name]
        if child.process is not None or time.monotonic() < child.next_start:
            return False
        try:
            if self.container and name in (BOT, WEB):
                from .credentials import validate_provider
                from wxbridge.config import load_account
                validate_provider(Path(load_account(self.root/'config.json').provider_env))
            env = self.environment(gui=name != SESSION)
            if name == WEB and child.process is None:
                self.web_saved = read_web(self.web_config)
                self.web_launch_settings = self.web_saved
                command = child.command
                command[command.index('--host')+1] = self.web_saved.host
                command[command.index('--port')+1] = str(self.web_saved.port)
                self.web_health_token = secrets.token_hex(16)
                env['WXBRIDGE_WEB_HEALTH_TOKEN'] = self.web_health_token
            return child.start(env, None if capture else self.output(name), capture=capture)
        except (OSError, ValueError) as exc:
            child.next_start = time.monotonic() + 15
            self.error = 'component_start_' + type(exc).__name__
            if name == WEB:self.web_error = 'web_start_failed'
            log.error('component_start_failed name=%s category=%s', name, type(exc).__name__)
            return False

    def status(self):
        components = {name: self.children[name].status() for name in COMPONENTS}
        components[RECOVERY].update(state='active' if self.enabled and not self.stopping else 'inactive',
                                    substate=self.phase, enabled=self.enabled, error=self.error)
        return dict(components=components, bot_enabled=self.enabled, phase=self.phase, error=self.error,
                    web=self.web_status())

    def web_status(self):
        error = self.web_error
        try:
            saved = read_web(self.web_config)
        except (OSError, ValueError):
            saved, error = self.web_saved, 'web_settings_invalid'
        child = self.children[WEB]
        return dict(**child.status(), enabled=self.web_enabled, saved=web_document(saved),
                    effective=web_document(self.web_effective), ready=self.web_ready and child.running and not child.stopping,
                    pending=saved != self.web_effective or self.web_applied_generation != self.web_generation,
                    generation=self.web_applied_generation, requested_generation=self.web_generation, error=error)

    def web_action(self, action):
        child = self.children[WEB]
        if action != 'web_stop':
            self.web_saved = read_web(self.web_config)
        previous = self.web_enabled
        if action != 'web_reload':
            self.web_enabled = action != 'web_stop'
        try:
            self.persist()
        except OSError:
            self.web_enabled = previous
            raise
        if action == 'web_start' and previous and child.running and not child.stopping:
            return dict(applied=False, generation=self.web_generation, message='Web 已启动。')
        self.web_generation += 1
        self.web_ready, self.web_error, self.web_next_probe = False, '', 0
        if action != 'web_start':
            child.stop()
        if child.process is None:
            child.next_start = 0
        return dict(applied=True, generation=self.web_generation,
                    message='Web 操作请求已接收；微信和机器人保持运行。')

    def probe_web(self):
        child = self.children[WEB]
        if not child.running or child.stopping or time.monotonic() < self.web_next_probe:
            return
        self.web_next_probe = time.monotonic()+1
        settings = self.web_launch_settings
        host = '['+settings.host+']' if ':' in settings.host else settings.host
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://{host}:{settings.port}/healthz", timeout=.2) as response:
                healthy = (response.status == 200 and response.read(128) == b'{"status":"ok"}'
                    and response.headers.get('X-WXBridge-Health') == self.web_health_token)
            self.web_ready = healthy
            if healthy:
                self.web_effective = settings
                self.web_applied_generation = self.web_generation
                self.web_ready, self.web_error = True, ''
            else:
                self.web_error = 'web_health_check_failed'
        except (OSError, ValueError, HTTPException):
            self.web_ready = False
            self.web_error = 'web_health_check_failed'

    def action(self, action, *, uid=0):
        if action not in ACTIONS:
            raise ValueError('invalid_control_action')
        if action == 'status':
            return self.status()
        if self.stopping:
            raise ValueError('service_stopping')
        if action in ('account_login','account_bind'):
            if not self.container:raise ValueError('container_onboarding_only')
            from .onboarding import login_snapshot, bind_configured_account
            return login_snapshot(self.root) if action=='account_login' else bind_configured_account(self)
        if action in WEB_ACTIONS:
            if uid != 0:
                raise ValueError('web_control_requires_root')
            return self.web_action(action)
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
        # systemd reapplies RuntimeDirectoryMode for each Exec command, so an
        # ExecStartPre chmod alone cannot make this socket reachable by Web.
        os.chown(parent, 0, self.account.pw_gid)
        os.chmod(parent, 0o750)
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
                    result = dict(ok=True, result=self.action(value['action'], uid=uid))
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
            if self.container and self.stopping and name in (BOT, HELPER) and code == -getattr(signal, 'SIGKILL', 9):
                self.forced_shutdown = True
            if name == SESSION:
                self.ready = False
                for other in (BOT, HELPER, RECOVERY):
                    self.children[other].stop(signal.SIGINT if other == BOT else signal.SIGTERM)
            elif name == HELPER:
                self.ready = False
                self.children[BOT].stop(signal.SIGINT)
            elif name == BOT:
                self.ready, self.checkpoint_needed = False, not self.container
            elif name == WEB:
                self.web_ready, self.web_effective = False, None
                if not intentional:
                    self.web_error = 'web_process_exited'
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
                if child.command[-1]=='directory':
                    self.ready=False
                    if code==0 or intentional:
                        child.command[-1]='prepare'
                        self.next_prepare=time.monotonic()
                        if code==0:self.error=''
                    else:
                        self.next_prepare=time.monotonic()+30
        bot = self.children[BOT]
        if not self.enabled or self.stopping:
            bot.stop(signal.SIGINT)
            recovery = self.children[RECOVERY]
            if self.stopping or recovery.command[-1] != 'directory':
                recovery.stop()
        if bot.process is None and self.checkpoint_needed and self.children['checkpoint'].process is None:
            self.start('checkpoint', capture=True)
        if self.stopping:
            if bot.process is None and not self.checkpoint_needed and self.children['checkpoint'].process is None:
                self.children[HELPER].stop()
                if self.container:
                    self.drain_container_helper()
                if self.children[HELPER].process is None:
                    self.children[SESSION].stop()
            return
        if self.web_enabled:
            self.start(WEB)
            self.probe_web()
        else:
            self.children[WEB].stop()
        if self.children[SESSION].process is None and bot.process is None and self.children[HELPER].process is None:
            self.start(SESSION)
        desktop_ready = self.children[SESSION].running and (self.run_dir/'env').is_file() and (self.run_dir/'wechat.pid').is_file()
        if desktop_ready:
            self.start(HELPER)
        else:
            self.ready = False
        if (desktop_ready and self.children[HELPER].running and bot.process is None
                and self.children[RECOVERY].command[-1]=='directory'
                and self.children[RECOVERY].process is None and time.monotonic() >= self.next_prepare):
            self.start(RECOVERY, capture=True)
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
                        remaining = self.container_processes() if self.container else self.remaining_processes()
                        if not remaining:
                            break
                        if orphan_deadline is None:
                            orphan_deadline = time.monotonic()+5
                        sig = signal.SIGTERM if time.monotonic()<orphan_deadline else signal.SIGKILL
                        if self.container and sig == signal.SIGKILL:
                            self.forced_shutdown = True
                        for pid in remaining:
                            if self.container:
                                from .processes import signal_pid
                                signal_pid(pid, sig)
                            else:
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
            for thread in getattr(self, 'log_threads', ()):
                thread.join(timeout=2)
            if self.lock_fd is not None:
                os.close(self.lock_fd)

    def drain_container_helper(self):
        from .processes import wechat_processes, signal_pid
        child = self.children[HELPER]
        if child.process is None:
            return
        now = time.monotonic()
        if self.helper_stop_started is None:
            self.helper_stop_started = now
        if now - self.helper_stop_started < 30:
            return
        clients = wechat_processes(self.account.pw_uid)
        if clients:
            if self.wechat_stop_started is None:
                self.wechat_stop_started = now
                self.forced_shutdown = True
                log.error('container_forced_wechat_shutdown')
            sig = signal.SIGTERM if now-self.wechat_stop_started < 15 else signal.SIGKILL
            for pid in clients:
                signal_pid(pid, sig)
        else:
            child._signal(signal.SIGKILL)

    @staticmethod
    def container_processes():
        from .processes import remaining_processes
        return remaining_processes()

    @staticmethod
    def remaining_processes():
        group = next((line.split(':',2)[2].strip() for line in Path('/proc/self/cgroup').read_text().splitlines()
                      if line.startswith('0::')), '')
        if not group.endswith('/wxbridge.service') or '..' in Path(group).parts:
            raise ValueError('supervisor_requires_own_systemd_cgroup')
        path = Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
        return [int(pid) for pid in path.read_text().split() if int(pid)!=os.getpid()]
