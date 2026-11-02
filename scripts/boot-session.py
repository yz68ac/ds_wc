"""图形会话前台进程；容器模式通过进程句柄清理已启动的桌面组件。"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import re
import select


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from wxbridge.host.layout import container
RUN = Path(os.environ.get('WX_RUNTIME_DIR', '/run/wxbridge-trial'))


def desktop_handles():
    handles = []
    pids = set()
    for name in ('wechat', 'xvfb', 'atspi', 'fluxbox', 'vnc'):
        path = RUN/(name+'.pid')
        if path.exists():
            pids.add(int(path.read_text().strip()))
    env = RUN/'dbus.env'
    if env.exists():
        match = re.search(r'DBUS_SESSION_BUS_PID=(\d+)', env.read_text())
        if match:
            pids.add(int(match[1]))
    for pid in pids:
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            fd = os.pidfd_open(pid)
            if Path(f'/proc/{pid}').stat().st_uid != os.geteuid():
                os.close(fd)
                continue
            handles.append(fd)
        except ProcessLookupError:
            pass
    return handles


def close_desktop(handles):
    poll = select.poll()
    try:
        for fd in handles:
            poll.register(fd, select.POLLIN)
            try:signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:pass
        pending = set(handles)
        deadline = time.monotonic()+5
        while pending and time.monotonic() < deadline:
            pending.difference_update(fd for fd, event in poll.poll(100))
        for fd in pending:
            try:signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:pass
    finally:
        for fd in handles:os.close(fd)


def main():
    subprocess.run(['/usr/bin/bash', str(ROOT/'scripts/start-headless.sh')], check=True)
    handles = desktop_handles() if container() else []
    pid = int((RUN/'wechat.pid').read_text())
    expected = Path('/opt/wechat/wechat').resolve()
    started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    python = ROOT/'.venv/bin/python'
    login = subprocess.Popen([str(python) if python.is_file() else sys.executable,
                              str(ROOT/'scripts/wechat-login.py'), '--background'])
    try:
        while True:
            try:
                if Path(f'/proc/{pid}/exe').resolve(strict=True) != expected:
                    return 1
                if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19] != started:
                    return 1
                os.kill(pid, 0)
            except (OSError, ValueError, IndexError):
                return 1
            time.sleep(5)
    finally:
        if login.poll() is None:
            login.terminate()
            try:
                login.wait(timeout=5)
            except subprocess.TimeoutExpired:
                login.kill()
                login.wait()
        if handles:
            close_desktop(handles)


if __name__ == '__main__':
    def stopped(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    raise SystemExit(main())
