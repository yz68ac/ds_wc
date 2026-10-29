"""systemd 图形会话前台进程；退出时由 control-group 清理其所有子进程。"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get('WX_RUNTIME_DIR', '/run/wxbridge-trial'))


def main():
    subprocess.run(['/usr/bin/bash', str(ROOT/'scripts/start-headless.sh')], check=True)
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


if __name__ == '__main__':
    def stopped(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    raise SystemExit(main())
