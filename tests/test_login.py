"""Login states, checked clicks and startup policy using a synthetic desktop only."""
from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_linux.desktop.login import LoginController, LoginError, LoginQRCode, LoginStatus


ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_module = script('login_runtime')
with patch.dict(sys.modules, login_runtime=runtime_module):
    viewer = script('wechat-login')


BOUNDS = dict(x=10, y=20, width=300, height=400)


class Node:
    def __init__(self, role, name='', *, parent=None, visible=True, enabled=True, pid=123):
        self.role, self.name, self.parent, self.pid = role, name, parent, pid
        self.states = {'visible', 'showing'} if visible else set()
        if enabled:
            self.states.add('enabled')
    def get_role_name(self): return self.role
    def get_parent(self): return self.parent
    def get_process_id(self): return self.pid
    def get_state_set(self): return SimpleNamespace(contains=lambda state: state in self.states)


def controller():
    value = object.__new__(LoginController)
    value.pid, value._start, value._boot = 123, '100', 'synthetic-boot'
    value.ui_options = {}
    value._process_start = Mock(return_value='100')
    value._database_state = Mock(return_value=(False, False))
    return value


class LoginControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = controller()
        self.frame = Node('frame', 'WeChat')

    def desktop(self, *nodes):
        desktop = Mock()
        desktop.get_child_count.return_value = 1
        desktop.get_child_at_index.return_value = Node('application')
        gui = SimpleNamespace(Atspi=SimpleNamespace(get_desktop=lambda _: desktop,
            StateType=SimpleNamespace(SHOWING='showing', VISIBLE='visible', ENABLED='enabled')),
            walk=lambda *args, **kwargs: [(n, n.role, n.name, BOUNDS) for n in nodes],
            _bounds=lambda n: dict(BOUNDS) if n is not None else None)
        self.controller._gui = Mock(return_value=gui)

    def test_database_identity_wins_and_wrong_account_blocks_actions(self):
        for opened, expected in (((True, False), 'logged_in'), ((True, True), 'unknown'),
                                 ((False, True), 'unknown')):
            with self.subTest(opened=opened):
                self.controller._database_state.return_value = opened
                self.assertEqual(self.controller.inspect().phase, expected)
        self.controller._process_start.return_value = '200'
        self.assertEqual(self.controller.inspect().phase, 'client_stopped')

    def test_recognizes_login_open_phone_qr_and_expired_qr(self):
        cases = [('push button', 'Log In', 'login_ready'),
                 ('button', 'Open WeChat', 'open_ready'),
                 ('label', 'Confirm login on your phone', 'phone_confirmation'),
                 ('image', 'Scan QR code', 'qr_required'),
                 ('label', '二维码已过期', 'qr_expired')]
        for role, name, expected in cases:
            with self.subTest(name=name):
                self.desktop(Node(role, name, parent=self.frame))
                self.assertEqual(self.controller.inspect().phase, expected)

    def test_hidden_disabled_duplicate_or_cross_window_controls_do_not_click(self):
        for nodes, expected in (([Node('button', 'Log In', parent=self.frame, visible=False)], 'unknown'),
            ([Node('button', 'Log In', parent=self.frame, enabled=False)], 'unknown'),
            ([Node('button', 'Log In', parent=self.frame), Node('button', 'Log In', parent=self.frame)], 'unknown'),
            ([Node('list', 'Chats', parent=self.frame), Node('button', 'Log In', parent=self.frame)], 'unknown')):
            with self.subTest(expected=expected, nodes=len(nodes)):
                self.desktop(*nodes)
                self.assertEqual(self.controller.inspect().phase, expected)
                with patch.object(self.controller, '_xdo') as click:
                    with self.assertRaises(LoginError): self.controller.click_login()
                    click.assert_not_called()

    def click_fixture(self):
        button = Node('button', 'Log In', parent=self.frame)
        self.controller._snapshot = Mock(return_value=(LoginStatus('login_ready', 123),
                                                       dict(buttons=dict(login=button))))
        self.controller._bounds = Mock(return_value=dict(BOUNDS))
        self.controller._window = Mock(return_value='42')
        self.controller._activate = Mock()
        self.controller._xdo = Mock()
        pointer = Mock()
        pointer.pointer_client.return_value = 42
        return pointer

    def test_checked_click_uses_verified_center_and_mouse_path(self):
        pointer = self.click_fixture()
        with patch('wechat_linux.desktop.ui.X11', return_value=pointer):
            self.controller.click_login()
        pointer.move_pointer.assert_called_once_with(160, 220)
        self.controller._xdo.assert_called_once_with('click', '1')

    def test_obstruction_or_ui_change_blocks_click_and_uncertain_click_is_explicit(self):
        pointer = self.click_fixture()
        with patch('wechat_linux.desktop.ui.X11', return_value=pointer):
            pointer.pointer_client.return_value = 99
            with self.assertRaisesRegex(LoginError, 'login_window_obstructed'):
                self.controller.click_login()
            self.controller._xdo.assert_not_called()
            pointer.pointer_client.return_value = 42
            self.controller._bounds.side_effect = [dict(BOUNDS), dict(BOUNDS, x=11)]
            with self.assertRaisesRegex(LoginError, 'login_ui_changed'):
                self.controller.click_login()
            self.controller._bounds.side_effect = None
            self.controller._xdo.side_effect = LoginError('login_window_operation_failed')
            with self.assertRaises(LoginError) as raised:
                self.controller.click_login()
            self.assertEqual(raised.exception.code, 'login_click_uncertain')
            self.assertIsNone(raised.exception.submitted)

    def test_read_qr_never_captures_a_non_qr_state(self):
        self.controller._database_state.return_value = (True, False)
        with patch.object(self.controller, '_activate') as activate:
            with self.assertRaisesRegex(LoginError, 'login_qr_unavailable'):
                self.controller.read_qr()
            activate.assert_not_called()


class LoginRuntimeTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.runtime = object.__new__(runtime_module.LoginRuntime)
        self.runtime.directory = Path(temporary.name)
        self.runtime.path = self.runtime.directory/'login-state.json'
        self.runtime.controller = Mock(pid=123, identity=dict(pid=123, start_time='100', boot_id='boot'))
        # Windows has no geteuid; synthetic files and state still exercise real serialization.
        uid = patch.object(runtime_module.os, 'geteuid', return_value=self.runtime.directory.stat().st_uid, create=True)
        uid.start()
        self.addCleanup(uid.stop)
        if os.name == 'nt':
            # Windows chmod does not implement POSIX owner-only mode bits.
            private = patch.object(runtime_module, 'private_file',
                side_effect=lambda path, **kwargs: path.read_text(encoding='utf-8'))
            private.start()
            self.addCleanup(private.stop)

    def phase(self, phase):
        self.runtime.controller.inspect.return_value = LoginStatus(phase, 123)

    def test_login_reserved_before_click_and_never_repeated_after_restart(self):
        self.phase('login_ready')
        def click():
            self.assertEqual(self.runtime.state()['attempted'], ['login'])
        self.runtime.controller.click_login.side_effect = click
        self.assertEqual(self.runtime.advance().phase, 'phone_confirmation')
        self.runtime.advance()
        self.runtime.controller.click_login.assert_called_once()
        self.assertEqual(self.runtime.state()['attempted'], ['login'])

    def test_uncertain_click_is_not_retried_and_open_action_has_separate_reservation(self):
        self.phase('login_ready')
        self.runtime.controller.click_login.side_effect = LoginError('login_click_uncertain', submitted=None)
        self.assertEqual(self.runtime.advance().reason, 'login_click_uncertain')
        self.runtime.advance()
        self.runtime.controller.click_login.assert_called_once()
        self.phase('open_ready')
        self.runtime.advance()
        self.runtime.advance()
        self.runtime.controller.open_wechat.assert_called_once()
        self.assertEqual(self.runtime.state()['attempted'], ['login', 'open'])

    def test_completion_stops_monitoring_and_new_process_resets_actions(self):
        self.phase('logged_in')
        self.assertEqual(self.runtime.advance().phase, 'logged_in')
        self.phase('login_ready')
        self.assertEqual(self.runtime.advance().phase, 'completed')
        self.runtime.controller.click_login.assert_not_called()
        self.runtime.controller.identity = dict(pid=123, start_time='200', boot_id='boot')
        self.runtime.advance()
        self.runtime.controller.click_login.assert_called_once()

    def test_first_login_requires_explicit_binding_and_never_claims_validated_identity(self):
        self.runtime.unbound = True
        self.runtime.controller.inspect.return_value = LoginStatus('unknown',123,'login_account_mismatch')
        self.assertEqual(self.runtime.advance().phase,'setup_required')
        self.assertFalse(self.runtime.state()['completed'])
        self.assertEqual(self.runtime.viewer_snapshot().phase,'setup_required')
        self.runtime.controller.click_login.assert_not_called()
        self.runtime.controller.open_wechat.assert_not_called()
        self.runtime.unbound = False
        self.assertEqual(self.runtime.inspect().phase,'unknown')
        self.assertEqual(self.runtime.inspect().reason,'login_account_mismatch')

    def test_login_summary_is_current_process_metadata_without_qr_content(self):
        self.phase('qr_required')
        self.runtime.advance()
        value=json.loads((self.runtime.directory/'login-status.json').read_text())
        self.assertEqual(value['phase'],'qr_required')
        self.assertEqual(value['pid'],123)
        self.assertEqual(value['process'],self.runtime.controller.identity)
        self.assertNotIn('data',value)

    def test_background_never_refreshes_and_viewer_refreshes_once_per_expiry(self):
        self.phase('qr_expired')
        self.runtime.advance()
        self.runtime.controller.refresh_qr.assert_not_called()
        self.runtime.viewer_snapshot()
        self.runtime.viewer_snapshot()
        self.runtime.controller.refresh_qr.assert_called_once()
        self.phase('qr_required')
        self.runtime.viewer_snapshot()
        self.phase('qr_expired')
        self.runtime.viewer_snapshot()
        self.assertEqual(self.runtime.controller.refresh_qr.call_count, 2)

    def test_failed_refresh_is_also_reserved_and_unknown_layout_has_no_action(self):
        self.phase('qr_expired')
        self.runtime.controller.refresh_qr.side_effect = LoginError('login_click_uncertain')
        self.assertEqual(self.runtime.viewer_snapshot().reason, 'login_click_uncertain')
        self.runtime.viewer_snapshot()
        self.runtime.controller.refresh_qr.assert_called_once()
        self.phase('unknown')
        self.runtime.advance()
        self.runtime.controller.click_login.assert_not_called()
        self.runtime.controller.open_wechat.assert_not_called()

    def test_invalid_saved_state_is_not_silently_reset_or_retried(self):
        value = dict(process=self.runtime.controller.identity, attempted=['switch_account'],
                     completed=False, qr_refreshed=False)
        self.runtime.path.write_text(json.dumps(value), encoding='utf-8')
        self.runtime.path.chmod(0o600)
        with self.assertRaisesRegex(LoginError, 'login_state_invalid'):
            self.runtime.advance()
        self.runtime.controller.click_login.assert_not_called()

    def test_nonobject_or_corrupt_state_reports_fixed_error_without_action(self):
        for raw in ('[]', 'null', '{'):
            with self.subTest(raw=raw):
                self.runtime.path.write_text(raw, encoding='utf-8')
                self.runtime.path.chmod(0o600)
                with self.assertRaisesRegex(LoginError, 'login_state_invalid'):
                    self.runtime.advance()
                self.runtime.controller.click_login.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX locking and owner-only permissions')
    def test_real_operation_lock_contention_and_private_state_permissions(self):
        with self.runtime.locked():
            with patch.object(runtime_module.time, 'monotonic', side_effect=[0, 4]):
                with self.assertRaisesRegex(LoginError, 'login_ui_busy'):
                    with self.runtime.locked(): self.fail('concurrent UI operation')
        self.phase('unknown')
        self.runtime.advance()
        self.assertEqual(self.runtime.path.stat().st_mode & 0o777, 0o600)
        self.runtime.path.chmod(0o644)
        with self.assertRaisesRegex(LoginError, 'login_runtime_permissions'):
            self.runtime.advance()


class LoginTerminalTests(unittest.TestCase):
    def test_qr_payload_is_not_in_repr_and_invalid_input_is_rejected(self):
        qr = LoginQRCode(b'SYNTHETIC_QR_SECRET')
        self.assertNotIn('SYNTHETIC_QR_SECRET', repr(qr))
        self.assertEqual(len(qr.fingerprint), 64)
        for payload in ('text', b'', b'x'*8193):
            with self.assertRaisesRegex(LoginError, 'login_qr_invalid'):
                LoginQRCode(payload)

    def test_half_blocks_keep_matrix_and_white_quiet_zone(self):
        qrcode = SimpleNamespace(QRCode=Mock(), constants=SimpleNamespace(ERROR_CORRECT_M=0))
        qrcode.QRCode.return_value.get_matrix.return_value = [[False, True, True],
                                                             [False, False, True], [False]*3]
        with patch.dict(sys.modules, qrcode=qrcode):
            width, lines = LoginQRCode(b'synthetic').terminal_lines()
        self.assertEqual(width, 3)
        self.assertEqual(lines, ('\x1b[30;107m ▀█\x1b[0m', '\x1b[30;107m   \x1b[0m'))
        self.assertEqual(qrcode.QRCode.call_args.kwargs['border'], 4)

    def test_noninteractive_terminal_is_rejected_before_capture_or_action(self):
        runtime = Mock()
        with patch.object(viewer.sys, 'stdout', io.StringIO()):
            with self.assertRaisesRegex(LoginError, 'login_terminal_required'):
                viewer.watch(runtime)
        runtime.locked.assert_not_called()

    def test_narrow_terminal_prints_required_size_without_qr_or_payload(self):
        runtime = Mock()
        runtime.locked.side_effect = lambda: nullcontext()
        runtime.viewer_snapshot.return_value = LoginStatus('qr_required', 123)
        runtime.controller.read_qr.return_value = SimpleNamespace(fingerprint='synthetic',
            terminal_lines=lambda: (40, ('PRIVATE_QR_LINE',)*20))
        output, input_ = io.StringIO(), Mock()
        output.isatty = lambda: True
        input_.isatty.return_value = True
        with patch.object(viewer.sys, 'stdout', output), patch.object(viewer.sys, 'stdin', input_), \
             patch.object(viewer.shutil, 'get_terminal_size', return_value=SimpleNamespace(columns=30, lines=20)), \
             patch.object(viewer.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): viewer.watch(runtime)
        self.assertIn('42 列、25 行', output.getvalue())
        self.assertNotIn('PRIVATE_QR_LINE', output.getvalue())
        self.assertTrue(output.getvalue().endswith('\x1b[?1049l'))
