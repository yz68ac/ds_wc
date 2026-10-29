"""Explicit login UI operations for an existing, caller-owned WeChat process."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import subprocess

from wechat_linux.errors import WeChatError


class LoginError(WeChatError):
    def __init__(self, code, *, submitted=False):
        super().__init__(code)
        self.code, self.submitted = code, submitted


@dataclass(frozen=True)
class LoginStatus:
    phase: str
    pid: int
    reason: str = ''


@dataclass(frozen=True)
class LoginQRCode:
    payload: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= 8192:
            raise LoginError('login_qr_invalid')

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload).hexdigest()

    def terminal_lines(self):
        """Return explicitly requested ANSI QR lines; never print or save them."""
        try:
            import qrcode
            code = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
            code.add_data(self.payload, optimize=0)
            code.make(fit=True)
            matrix = code.get_matrix()
        except ImportError:
            raise LoginError('login_qr_dependency_missing') from None
        except Exception:
            raise LoginError('login_qr_render_failed') from None
        width = len(matrix)
        lines = []
        for y in range(0, width, 2):
            lower = matrix[y + 1] if y + 1 < width else [False] * width
            cells = {(False, False): ' ', (True, False): '▀',
                     (False, True): '▄', (True, True): '█'}
            line = ''.join(cells[bool(a), bool(b)] for a, b in zip(matrix[y], lower))
            lines.append('\x1b[30;107m' + line + '\x1b[0m')
        return width, tuple(lines)


LOGIN = frozenset({'log in', 'login', '登录'})
OPEN = frozenset({'open wechat', '进入微信', '打开微信'})
REFRESH = frozenset({'refresh', 'refresh qr code', 'refresh qrcode', '刷新',
                     '点击刷新', '重新获取二维码', '刷新二维码'})
BUTTON_ROLES = frozenset({'button', 'push button'})


class LoginController:
    """Observe or act on a verified login window. The caller owns retry policy.

    Construction binds the PID and start time, but performs no desktop actions.
    No process, thread, file, service, account switch or network request is started.
    """

    def __init__(self, pid, db_dir, *, ui_options=None, executable='/opt/wechat/wechat'):
        if type(pid) is not int or pid <= 0:
            raise LoginError('login_invalid_process')
        database = Path(db_dir).expanduser()
        if not database.is_absolute() or database.name != 'db_storage':
            raise LoginError('login_invalid_database_directory')
        self.pid, self.db_dir = pid, database.resolve()
        self.executable = Path(executable).resolve()
        self.ui_options = dict(ui_options or {})
        self._a11y = None
        self._start = self._process_start()
        try:
            self._boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            raise LoginError('login_process_unavailable') from None

    @property
    def identity(self):
        return dict(pid=self.pid, start_time=self._start, boot_id=self._boot)

    def _process_start(self):
        process = Path('/proc') / str(self.pid)
        try:
            if (process.stat().st_uid != os.geteuid()
                    or (process / 'exe').resolve(strict=True) != self.executable):
                raise LoginError('login_wrong_process')
            return (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        except (OSError, IndexError):
            raise LoginError('login_process_unavailable') from None

    def _database_state(self):
        expected, other = False, False
        try:
            for fd in (Path('/proc') / str(self.pid) / 'fd').iterdir():
                try:
                    target = fd.resolve(strict=True)
                except OSError:
                    continue
                if target.suffix == '.db' and 'db_storage' in target.parts:
                    if target.is_relative_to(self.db_dir):
                        expected = True
                    else:
                        other = True
        except OSError:
            raise LoginError('login_process_unavailable') from None
        return expected, other

    def _early_status(self):
        try:
            if self._process_start() != self._start:
                return LoginStatus('client_stopped', self.pid, 'login_process_changed')
        except LoginError as error:
            return LoginStatus('client_stopped', self.pid, error.code)
        expected, other = self._database_state()
        if other:
            return LoginStatus('unknown', self.pid, 'login_account_mismatch')
        if expected:
            return LoginStatus('logged_in', self.pid)
        return None

    def _gui(self):
        if self._a11y is None:
            from wechat_linux.desktop.ui import A11y
            self._a11y = A11y(self.ui_options)
        if not self._a11y.available:
            raise LoginError('login_accessibility_unavailable')
        return self._a11y

    def _bounds(self, node):
        bounds = self._gui()._bounds(node)
        if not bounds or bounds['width'] > 4096 or bounds['height'] > 4096:
            return None
        return bounds

    def _frame(self, node):
        for _ in range(25):
            if node.get_role_name() in ('frame', 'window', 'dialog') and self._bounds(node):
                return node
            node = node.get_parent()
            if node is None:
                break
        return None

    def _snapshot(self):
        early = self._early_status()
        if early is not None:
            return early, {}
        gui = self._gui()
        try:
            desktop = gui.Atspi.get_desktop(0)
            apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
            apps = [app for app in apps if app and app.get_process_id() == self.pid]
            if len(apps) != 1:
                return LoginStatus('waiting', self.pid, 'login_application_unavailable'), {}
            visible = []
            for node, role, name, bounds in gui.walk(apps[0], max_depth=14, max_nodes=400):
                states = node.get_state_set()
                if (bounds and states.contains(gui.Atspi.StateType.SHOWING)
                        and states.contains(gui.Atspi.StateType.VISIBLE)):
                    visible.append((node, role, (name or '').strip().casefold(), states))
            # A main chat window with no expected-account DB is not a login page.
            if any(role == 'list' and name in ('chats', '聊天', '会话') for _, role, name, _ in visible):
                return LoginStatus('unknown', self.pid, 'login_main_window_without_account'), {}
            buttons = {}
            for key, labels in (('login', LOGIN), ('open', OPEN), ('refresh', REFRESH)):
                matches = [node for node, role, name, states in visible if role in BUTTON_ROLES
                           and name in labels and states.contains(gui.Atspi.StateType.ENABLED)]
                if len(matches) > 1:
                    return LoginStatus('unknown', self.pid, 'login_controls_ambiguous'), {}
                if matches:
                    buttons[key] = matches[0]
            names = [name for _, _, name, _ in visible]
            qr_nodes = [node for node, role, name, _ in visible
                        if role in ('label', 'text', 'static', 'image', 'icon', 'button', 'push button')
                        and any(word in name for word in ('qr code', 'qrcode', '二维码'))]
            expired = any(any(word in name for word in ('expired', '已失效', '已过期', '二维码失效'))
                          for name in names)
            phone = any(any(word in name for word in ('confirm on your phone', 'confirm login on your phone',
                        'confirm the login on your phone', '请在手机上确认', '请在手机微信上确认')) for name in names)
            if 'open' in buttons:
                phase, anchor = 'open_ready', buttons['open']
            elif phone:
                return LoginStatus('phone_confirmation', self.pid), {}
            elif qr_nodes:
                phase, anchor = ('qr_expired' if expired else 'qr_required'), qr_nodes[0]
            elif 'login' in buttons:
                phase, anchor = 'login_ready', buttons['login']
            else:
                return LoginStatus('unknown', self.pid, 'login_layout_unrecognized'), {}
            frame = self._frame(anchor)
            if frame is None:
                return LoginStatus('unknown', self.pid, 'login_window_unavailable'), {}
            frame_bounds = self._bounds(frame)
            anchors = list(buttons.values()) + qr_nodes
            if any(self._bounds(self._frame(node)) != frame_bounds for node in anchors):
                return LoginStatus('unknown', self.pid, 'login_controls_ambiguous'), {}
            return LoginStatus(phase, self.pid), dict(buttons=buttons, frame=frame)
        except LoginError:
            raise
        except Exception:
            raise LoginError('login_accessibility_failed') from None

    def inspect(self):
        return self._snapshot()[0]

    @staticmethod
    def _xdo(*args):
        try:
            return subprocess.run(['/usr/bin/xdotool', *map(str, args)], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise LoginError('login_window_operation_failed') from None

    def _window(self, bounds):
        windows = []
        try:
            for window in self._xdo('search', '--onlyvisible', '--pid', self.pid).splitlines():
                if int(self._xdo('getwindowpid', window)) != self.pid:
                    continue
                geometry = dict(line.split('=', 1) for line in
                                self._xdo('getwindowgeometry', '--shell', window).splitlines())
                left, top, width, height = (int(geometry[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT'))
                contains = (left <= bounds['x'] and top <= bounds['y']
                            and bounds['x'] + bounds['width'] <= left + width
                            and bounds['y'] + bounds['height'] <= top + height)
                same_frame = sum(abs(a - b) for a, b in zip(
                    (left, top, width, height),
                    (bounds['x'], bounds['y'], bounds['width'], bounds['height']))) <= 10
                if contains or same_frame:
                    windows.append(window)
        except (ValueError, KeyError):
            raise LoginError('login_window_unavailable') from None
        if len(windows) != 1:
            raise LoginError('login_window_ambiguous')
        return windows[0]

    def _activate(self, window):
        self._xdo('windowactivate', '--sync', window)
        self._xdo('windowraise', window)
        if int(self._xdo('getactivewindow')) != int(window):
            raise LoginError('login_window_obstructed')

    def _click(self, action, phase):
        try:
            self._click_checked(action, phase)
        except LoginError:
            raise
        except Exception:
            raise LoginError('login_accessibility_failed') from None

    def _click_checked(self, action, phase):
        status, view = self._snapshot()
        button = view.get('buttons', {}).get(action)
        if status.phase != phase or button is None:
            raise LoginError('login_action_unavailable')
        bounds = self._bounds(button)
        if bounds is None:
            raise LoginError('login_control_unavailable')
        window = self._window(bounds)
        self._activate(window)
        from wechat_linux.desktop.ui import X11
        pointer = X11(self.ui_options)
        pointer.move_pointer(bounds['x'] + bounds['width'] // 2,
                             bounds['y'] + bounds['height'] // 2)
        hit = pointer.pointer_client()
        if hit is None or int(hit) != int(window):
            raise LoginError('login_window_obstructed')
        fresh, current = self._snapshot()
        current_button = current.get('buttons', {}).get(action)
        if (fresh.phase != phase or current_button is None
                or self._bounds(current_button) != bounds or self._early_status() is not None):
            raise LoginError('login_ui_changed')
        try:
            self._xdo('click', '1')
        except LoginError:
            raise LoginError('login_click_uncertain', submitted=None) from None

    def click_login(self):
        self._click('login', 'login_ready')

    def open_wechat(self):
        self._click('open', 'open_ready')

    def refresh_qr(self):
        self._click('refresh', 'qr_expired')

    def read_qr(self):
        """Capture only the verified login window, returning decoded QR bytes."""
        status, view = self._snapshot()
        if status.phase != 'qr_required':
            raise LoginError('login_qr_unavailable')
        bounds = self._bounds(view['frame'])
        if bounds is None or bounds['width'] * bounds['height'] > 4 * 1024 * 1024:
            raise LoginError('login_capture_unavailable')
        window = self._window(bounds)
        self._activate(window)
        if self.inspect().phase != 'qr_required':
            raise LoginError('login_ui_changed')
        try:
            import gi
            gi.require_version('Gdk', '3.0')
            gi.require_version('GdkX11', '3.0')
            from gi.repository import Gdk, GdkX11
            import zxingcpp
            display = Gdk.Display.get_default() or Gdk.Display.open(os.environ.get('DISPLAY'))
            if display is None:
                raise LoginError('login_capture_unavailable')
            native = GdkX11.X11Window.foreign_new_for_display(display, int(window))
            if native is None:
                raise LoginError('login_capture_unavailable')
            width, height = native.get_width(), native.get_height()
            if width <= 0 or height <= 0 or width * height > 4 * 1024 * 1024:
                raise LoginError('login_capture_unavailable')
            image = Gdk.pixbuf_get_from_window(native, 0, 0, width, height)
            if image is None:
                raise LoginError('login_capture_unavailable')
            data, stride, channels = image.get_pixels(), image.get_rowstride(), image.get_n_channels()
            # A shaped grayscale buffer avoids Pillow, NumPy and OpenCV dependencies.
            gray = bytes((data[y * stride + x * channels] * 77
                          + data[y * stride + x * channels + 1] * 150
                          + data[y * stride + x * channels + 2] * 29) >> 8
                         for y in range(height) for x in range(width))
            pixels = memoryview(gray).cast('B', shape=(height, width))
            decoded = zxingcpp.read_barcodes(pixels, formats=zxingcpp.BarcodeFormat.QRCode)
            payloads = {bytes(item.bytes) for item in decoded if item.valid}
            if len(payloads) != 1:
                raise LoginError('login_qr_not_unique' if payloads else 'login_qr_not_found')
            payload = payloads.pop()
            if not 1 <= len(payload) <= 8192:
                raise LoginError('login_qr_invalid')
        except ImportError:
            raise LoginError('login_qr_dependency_missing') from None
        except LoginError:
            raise
        except Exception:
            raise LoginError('login_qr_decode_failed') from None
        fresh, current = self._snapshot()
        if (fresh.phase != 'qr_required' or self._bounds(current['frame']) != bounds
                or self._window(bounds) != window):
            raise LoginError('login_ui_changed')
        return LoginQRCode(payload)
