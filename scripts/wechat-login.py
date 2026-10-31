"""Advance boot login, or explicitly view the current login QR in an SSH TTY."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time

from login_runtime import LoginError, LoginRuntime, background


MESSAGES = {
    'logged_in': '微信已登录，账号数据库已打开。',
    'completed': '本次微信进程已完成自动登录。',
    'login_ready': '等待启动层发起登录；本命令不会重复发送登录请求。',
    'open_ready': '等待启动层进入微信。',
    'phone_confirmation': '请在手机微信确认登录。',
    'qr_required': '请用手机微信扫描二维码。',
    'qr_expired': '二维码已过期，正在等待刷新；必要时查看登录界面。',
    'client_stopped': '微信进程已结束，请重新运行本命令连接新会话。',
    'waiting_manual': '自动动作已尝试，等待手动确认或处理。',
    'opening_wechat': '正在进入微信。',
    'unknown': '暂时无法确认登录界面，不执行点击。',
    'waiting': '等待微信登录界面就绪。',
    'setup_required': '首次登录已打开账号数据库，请运行 sudo wxbridge setup 绑定账号。',
}


def watch(runtime):
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        raise LoginError('login_terminal_required')
    alternate = False
    previous = None
    try:
        while True:
            qr = None
            try:
                with runtime.locked():
                    status = runtime.viewer_snapshot()
                    if status.phase == 'qr_required':
                        qr = runtime.controller.read_qr()
                reason = status.reason
            except LoginError as error:
                status = runtime.controller.inspect() if error.code == 'login_ui_changed' else None
                reason = error.code
            if status is not None and status.phase in ('logged_in', 'client_stopped', 'setup_required'):
                if status.phase == 'setup_required':
                    sys.stdout.write(MESSAGES['setup_required']+'\n')
                return 0 if status.phase in ('logged_in', 'setup_required') else 1
            size = shutil.get_terminal_size()
            lines = ()
            if qr is not None:
                width, lines = qr.terminal_lines()
                if size.columns < width + 2 or size.lines < len(lines) + 5:
                    reason = '终端至少需要 ' + str(width + 2) + ' 列、' + str(len(lines) + 5) + ' 行，请调整窗口大小。'
                    lines = ()
            signature = (status.phase if status else None, reason,
                         qr.fingerprint if qr else None, size.columns, size.lines)
            if signature != previous:
                if not alternate:
                    sys.stdout.write('\x1b[?1049h\x1b[?25l')
                    alternate = True
                sys.stdout.write('\x1b[0m\x1b[H\x1b[2J')
                phase = status.phase if status else 'waiting'
                sys.stdout.write(MESSAGES.get(phase, MESSAGES['waiting']) + '\n')
                if reason:
                    sys.stdout.write(reason + '\n')
                for line in lines:
                    sys.stdout.write(line + '\n')
                sys.stdout.write('\x1b[0m\nCtrl+C 结束查看，微信继续运行。\n')
                sys.stdout.flush()
                previous = signature
            time.sleep(2)
    finally:
        if alternate:
            sys.stdout.write('\x1b[0m\x1b[H\x1b[2J\x1b[?25h\x1b[?1049l')
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--background', action='store_true', help='Startup-owned login loop; fixed metadata only')
    mode.add_argument('--status', action='store_true', help='Read-only JSON status; never capture or click')
    parser.add_argument('--runtime-dir', help='Existing private runtime directory')
    args = parser.parse_args()
    if sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8')
    if not args.background:
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    try:
        runtime = LoginRuntime(args.runtime_dir)
        if args.background:
            return background(runtime)
        if args.status:
            with runtime.locked():
                status = runtime.inspect()
            print(json.dumps(dict(phase=status.phase, reason=status.reason, pid=status.pid)))
            return 0
        result = watch(runtime)
        print(('请运行 sudo wxbridge setup 绑定账号。' if runtime.unbound else '微信已登录。') if result == 0 else '微信进程已结束。')
        return result
    except KeyboardInterrupt:
        return 130
    except LoginError as error:
        print('login: ' + error.code, file=sys.stderr)
    except Exception:
        print('login: login_initialization_failed', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
