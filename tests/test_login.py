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


from wxbridge.host import login as runtime_module
viewer = script('wechat-login')


BOUNDS = dict(x=10, y=20, width=300, height=400)








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
