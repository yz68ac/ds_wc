"""curses 只读终端面板；Linux 标准库，无额外运行依赖。"""
from __future__ import annotations

import locale
import sys
import time
import unicodedata

from wxbridge.app.monitor import PAGES, StatusReader, pages

HELP = ['键盘操作', '', '1–5：切换页面；Tab / 左右方向键：前后页',
        '上下方向键 / PgUp / PgDn：滚动；g / G：顶部 / 底部',
        '空格：暂停 / 恢复自动刷新；r：立即刷新（暂停时也可使用）',
        '? / h：帮助；q / Esc / Ctrl+C：退出面板', '',
        '只读状态文件与安全事件，systemd 每五秒采集一次。',
        '退出面板只结束查看。机器人由原服务继续运行。',
        '状态心跳超过十秒会标记过期，避免把旧快照当成在线。']


def clip(text, width):
    result, used = [], 0
    for c in text:
        if unicodedata.category(c).startswith('C'):
            continue
        size = 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
        if used+size > max(0, width):
            break
        result.append(c)
        used += size
    return ''.join(result)


def wrap_lines(lines, width):
    result = []
    width = max(2, width)
    for text in lines:
        line, used = '', 0
        for c in text:
            if unicodedata.category(c).startswith('C'):
                continue
            size = 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
            if used+size > width:
                result.append(line)
                line, used = '', 0
            line, used = line+c, used+size
        result.append(line)
    return result


def screen_loop(screen, reader, interval):
    import curses
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(150)
    page, scroll, paused, help_open = 0, 0, False, False
    snapshot, due, dirty, old_size = None, 0, True, None
    while True:
        tick = time.monotonic()
        if snapshot is None or (not paused and tick >= due):
            snapshot = reader.read()
            due, dirty = tick+interval, True
        height, width = screen.getmaxyx()
        if old_size != (height, width):
            old_size, dirty = (height, width), True
        body = wrap_lines(HELP if help_open else pages(snapshot, reader.cfg)[page], width-1)
        space = max(1, height-6)
        maximum = max(0, len(body)-space)
        scroll = max(0, min(scroll, maximum))
        if dirty:
            screen.erase()
            def put(row, text, attribute=0):
                if 0 <= row < height:
                    try:
                        screen.addstr(row, 0, clip(text, width-1), attribute)
                    except curses.error:
                        pass  # 窗口正在缩放，下一帧重画。
            put(0, 'wxbridge 运行面板  [只读]'+('  [已暂停采集]' if paused else ''), curses.A_BOLD)
            put(1, '  '.join(f'{i+1}:{label}'+(' *' if i == page else '') for i, label in enumerate(PAGES)))
            if width < 48 or height < 10:
                put(3, '终端较小，请放大窗口；q 退出')
            else:
                for row, line in enumerate(body[scroll:scroll+space], 3):
                    put(row, line)
            put(height-2, f'行 {scroll+1}–{min(len(body), scroll+space)} / {len(body)}  '
                f'采集 {time.strftime("%H:%M:%S", time.localtime(snapshot["collected_at"]))}')
            put(height-1, '1–5切页  ↑↓滚动  空格暂停  r刷新  ?帮助  q退出')
            screen.refresh()
            dirty = False
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ('q', 'Q', '\x1b', '\x03'):
            return 0
        if isinstance(key, str) and len(key) == 1 and key in '12345':
            page, scroll, help_open = int(key)-1, 0, False
        elif key in ('\t', curses.KEY_RIGHT):
            page, scroll, help_open = (page+1) % len(PAGES), 0, False
        elif key in (curses.KEY_BTAB, curses.KEY_LEFT):
            page, scroll, help_open = (page-1) % len(PAGES), 0, False
        elif key in ('?', 'h'):
            help_open, scroll = not help_open, 0
        elif key == ' ':
            paused = not paused
            due = 0
        elif key in ('r', 'R'):
            snapshot = reader.read(force=True)
            due = time.monotonic()+interval
        elif key in (curses.KEY_UP, 'k'):
            scroll -= 1
        elif key in (curses.KEY_DOWN, 'j'):
            scroll += 1
        elif key == curses.KEY_PPAGE:
            scroll -= space
        elif key == curses.KEY_NPAGE:
            scroll += space
        elif key in ('g', curses.KEY_HOME):
            scroll = 0
        elif key in ('G', curses.KEY_END):
            scroll = maximum
        dirty = True


def run_status(cfg, *, once=False, interval=1):
    reader = StatusReader(cfg)
    if once:
        view = reader.read(force=True)
        for name, lines in zip(PAGES, pages(view, cfg)):
            print(f'\n=== {name} ===\n'+'\n'.join(lines))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('交互面板需要终端。请通过 ssh -t 连接，或使用 status --once 查看一次。', file=sys.stderr)
        return 2
    try:
        import curses
    except ImportError:
        print('交互面板需要 Linux Python curses；可使用 status --once。', file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, '')
    if locale.getencoding().lower() in ('ascii', 'ansi_x3.4-1968'):
        locale.setlocale(locale.LC_ALL, 'C.UTF-8')
    try:
        return curses.wrapper(screen_loop, reader, interval)
    except curses.error:
        print('终端初始化失败，请检查 TERM，或使用 status --once。', file=sys.stderr)
        return 2
