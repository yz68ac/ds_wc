"""systemd 图形会话前台进程；退出时由 control-group 清理其所有子进程。"""
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get('WX_RUNTIME_DIR', '/run/wxbridge-trial'))


def main():
    subprocess.run(['/usr/bin/bash', str(ROOT/'scripts/start-headless.sh')], check=True)
    pid = int((RUN/'wechat.pid').read_text())
    expected = Path('/opt/wechat/wechat').resolve()
    while True:
        try:
            if Path(f'/proc/{pid}/exe').resolve(strict=True) != expected:
                return 1
            os.kill(pid, 0)
        except (OSError, ValueError):
            return 1
        time.sleep(5)


if __name__ == '__main__':
    def stopped(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    raise SystemExit(main())
