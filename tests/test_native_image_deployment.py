"""Optional helper assets and safe native callback drain; no service mutations."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'scripts'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deployment = script('deploy-release.py')
installer = script('install-native-helper.py')


class NativeImageDeploymentTests(unittest.TestCase):
    def test_old_installations_are_rejected_before_service_control(self):
        with patch.object(deployment,'run',side_effect=subprocess.CalledProcessError(1,['metadata'])) as run:
            with self.assertRaisesRegex(ValueError,'application_environment_upgrade_required'):
                deployment.require_application_layout()
        self.assertEqual(run.call_count,1)
        with patch.object(deployment,'run',side_effect=[Mock(),Mock(stdout='python scripts/service.py run')]):
            with self.assertRaisesRegex(ValueError,'service_entry_upgrade_required'):
                deployment.require_application_layout()

    def test_packaged_application_and_module_service_entry_are_accepted(self):
        with patch.object(deployment,'run',side_effect=[Mock(),Mock(stdout='python -m wxbridge_service --root /opt/wxbridge')]):
            deployment.require_application_layout()

    def setUp(self):
        (ROOT/'.test-tmp').mkdir(exist_ok=True)

    def test_socket_startup_race_only_repeats_readiness_without_sending(self):
        with patch.object(deployment, 'run', side_effect=[subprocess.CalledProcessError(1, ['readiness']), Mock()]) as run, \
                patch.object(deployment.time, 'sleep') as sleep:
            deployment.wait_native_ready()
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all('readiness(c)' in call.args[-1] for call in run.call_args_list))
        sleep.assert_called_once_with(.25)

    def test_legacy_installation_and_config_do_not_enable_or_require_images(self):
        cfg = dict(uid=995, allowed_chat_ids=['test@chatroom'], assets='/trusted',
                   socket='/run/test/helper.sock', worker_timeout_seconds=15)
        before = copy.deepcopy(cfg)
        self.assertEqual(deployment.image_configuration(cfg), before)
        self.assertEqual(cfg, before)
        self.assertIsNone(installer.image_options(False, None, None, 995))
        with self.assertRaisesRegex(ValueError, 'require_enable'):
            deployment.image_configuration(cfg, account='wxid_test')

    def test_explicit_account_binding_preserves_text_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.test-tmp') as folder:
            path = Path(folder)/'wxid_test'
            path.mkdir()
            uid = path.stat().st_uid
            cfg = dict(uid=uid, allowed_chat_ids=['test@chatroom'], assets='/trusted',
                       socket='/run/test/helper.sock', worker_timeout_seconds=15)
            result = deployment.image_configuration(cfg, enable=True, account='wxid_test', account_root=path)
            self.assertEqual({k: v for k, v in result.items() if k != 'image_download'}, cfg)
            self.assertEqual(result['image_download'], dict(enabled=True, expected_account='wxid_test',
                             account_root=str(path), timeout_seconds=30))
            self.assertNotIn('image_download', cfg)
            with self.assertRaisesRegex(ValueError, 'root_invalid'):
                installer.image_options(True, 'wxid_test', path, uid+1)
            with self.assertRaisesRegex(ValueError, 'root_invalid'):
                installer.image_options(True, 'wxid_other', path, uid)
            result['image_download']['timeout_seconds'] = 20
            with self.assertRaisesRegex(ValueError, 'change_requires_review'):
                deployment.image_configuration(result, enable=True, account='wxid_test', account_root=path)

    def test_optional_assets_are_installed_even_when_app_manifest_has_not_changed(self):
        files = {'wechat_linux/native/'+name: dict(sha256='a'*64)
                 for name in deployment.ASSETS+deployment.IMAGE_ASSETS}
        manifest = dict(files=files)
        self.assertEqual(deployment.helper_changes(manifest, manifest), [])
        old = dict(files={name: 'a'*64 for name in deployment.ASSETS})
        self.assertEqual(deployment.image_helper_changes(manifest, old), list(deployment.IMAGE_ASSETS))
        old['files'].update({name: 'a'*64 for name in deployment.IMAGE_ASSETS})
        self.assertEqual(deployment.image_helper_changes(manifest, old), [])
        old['files']['image_adapter.c'] = 'b'*64
        self.assertEqual(deployment.image_helper_changes(manifest, old), ['image_adapter.c'])

    def test_image_stop_requires_non_killing_policy_and_has_no_python_deadline(self):
        for line in installer.IMAGE_RUNTIME_POLICY.splitlines():
            self.assertIn(line.encode(), deployment.IMAGE_STOP_POLICY)
        self.assertIn('RuntimeDirectoryPreserve=yes', installer.IMAGE_RUNTIME_POLICY)
        with patch.object(deployment, 'active', return_value=True), \
                patch.object(deployment, 'run', return_value=Mock(stdout='TimeoutStopUSec=infinity\nSendSIGKILL=no\n')), \
                patch.object(deployment.subprocess, 'run') as stop:
            deployment.stop_helper(images_enabled=True)
            self.assertIsNone(stop.call_args.kwargs['timeout'])
            self.assertEqual(stop.call_args.args[0], ['systemctl', 'stop', deployment.UNITS[0]])
        for policy in ('TimeoutStopUSec=8s\nSendSIGKILL=yes\n', 'TimeoutStopUSec=infinity\nSendSIGKILL=yes\n'):
            with self.subTest(policy=policy), patch.object(deployment, 'active', return_value=True), \
                    patch.object(deployment, 'run', return_value=Mock(stdout=policy)), \
                    patch.object(deployment.subprocess, 'run') as stop:
                with self.assertRaisesRegex(ValueError, 'stop_policy_unsafe'):
                    deployment.stop_helper(images_enabled=True)
                stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
