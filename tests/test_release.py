"""发布包只取已提交代码，校验篡改、私密文件与路径穿越。"""
import io
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
release = runpy.run_path(str(ROOT/'scripts'/'release.py'))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'.test-tmp'
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'repo'
        self.root.mkdir()
        self.bundle = Path(self.temp.name)/'release.zip'
        self.git('init', '-q')
        for name in ('config.defaults.json', 'src/wxbridge/__main__.py', 'src/provider/__init__.py',
                     'src/wechat_linux/__init__.py', 'scripts/test-offline.py', 'prompts/system.md'):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else 'synthetic', encoding='utf-8')
        self.commit()

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.com',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=', *args], cwd=self.root,
            check=True, capture_output=True).stdout

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic')

    def build(self):
        return release['build'](self.bundle, self.root)

    def test_clean_committed_release_is_verified_and_not_overwritten(self):
        manifest = self.build()
        self.assertEqual(release['verify'](self.bundle), manifest)
        self.assertEqual(manifest['commit'], self.git('rev-parse', 'HEAD').decode().strip())
        with self.assertRaises(FileExistsError):
            self.build()

    def test_uncommitted_and_untracked_changes_block_release(self):
        path = self.root/'README.md'
        path.write_text('untracked', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'commit_changes'):
            self.build()
        self.commit()
        path.write_text('modified', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'commit_changes'):
            self.build()

    def test_committed_private_file_or_credential_is_rejected(self):
        for name, body in (('config.json', 'synthetic'), ('README.md', 'sk-'+'X'*28)):
            with self.subTest(name=name):
                path = self.root/name
                path.write_text(body, encoding='utf-8')
                self.commit()
                with self.assertRaisesRegex(ValueError, 'private_release_content'):
                    self.build()
                self.assertFalse(self.bundle.exists())
                path.unlink()
                self.commit()

    def altered(self, mutate):
        self.build()
        with zipfile.ZipFile(self.bundle) as bundle:
            files = {name: bundle.read(name) for name in bundle.namelist()}
        mutate(files)
        result = io.BytesIO()
        with zipfile.ZipFile(result, 'w') as bundle:
            for name, body in files.items():
                bundle.writestr(name, body)
        result.seek(0)
        return result

    def test_tampered_source_fails_hash_validation(self):
        data = self.altered(lambda files: files.update({'src/wxbridge/__main__.py': b'tampered'}))
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            release['verify'](data)

    def test_compiled_frontend_is_added_to_the_same_verified_release(self):
        globals_=release['build'].__globals__
        assets={'src/webui/dist/index.html':b'<html>synthetic</html>',
                'src/webui/dist/assets/app.js':b'console.log("synthetic")'}
        def frontend(archive):
            # Building the UI rereads source entries after they were copied into the output ZIP.
            for info in archive.infolist():
                if not info.is_dir():
                    self.assertEqual(archive.read(info.filename),(self.root/info.filename).read_bytes())
            return assets
        with patch.dict(globals_,frontend_files=frontend):
            manifest=self.build()
        self.assertTrue(set(assets)<=set(manifest['files']))
        self.assertEqual(release['verify'](self.bundle),manifest)

    def test_traversal_archive_is_rejected_before_extracting(self):
        data = self.altered(lambda files: files.update({'../outside': b'synthetic'}))
        with self.assertRaisesRegex(ValueError, 'invalid_release_path'):
            release['verify'](data)

    def test_clean_wheel_snapshot_excludes_old_builds_and_private_runtime(self):
        builder=runpy.run_path(str(ROOT/'scripts/build-wheels.py'))
        for name in ('build/lib/wechat_linux/retired.py','instance/private/config.json',
                     'src/provider/.env','src/wxbridge/__pycache__/old.pyc'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'old or private')
        (self.root/'.gitignore').write_text('build/\ninstance/\n.env\n__pycache__/\n')
        retired=self.root/'src/wxbridge/retired.py';retired.write_text('retired module')
        self.commit()
        retired.unlink()
        target=Path(self.temp.name)/'clean';target.mkdir()
        builder['stage_source'](self.root,target)
        self.assertTrue((target/'src/wxbridge/__main__.py').exists())
        self.assertFalse((target/'build').exists())
        self.assertFalse((target/'instance').exists())
        self.assertFalse((target/'src/provider/.env').exists())
        self.assertFalse((target/'src/wxbridge/retired.py').exists())

    def test_installed_module_mismatch_does_not_claim_the_manifest_revision(self):
        from wxbridge.release_integrity import installed_mismatches
        from wxbridge.app import build_info
        package=self.root/'installed';package.mkdir()
        module=package/'control.py';module.write_bytes(b'new')
        manifest=dict(commit='a'*40,files={'src/wxbridge/control.py':dict(sha256=hashlib.sha256(b'new').hexdigest())})
        (self.root/'release-manifest.json').write_text(json.dumps(manifest))
        locations={'wxbridge':package}
        self.assertEqual(installed_mismatches(manifest['files'],locations=locations),[])
        defaults=ROOT/'config.defaults.json'
        resource={'config.defaults.json':dict(sha256=hashlib.sha256(defaults.read_bytes()).hexdigest())}
        self.assertEqual(installed_mismatches(resource,locations=locations),[])
        self.assertEqual(installed_mismatches(resource,locations=locations,data_root=package),['config.defaults.json'])
        module.write_bytes(b'old')
        failures=installed_mismatches(manifest['files'],locations=locations)
        self.assertEqual(failures,['src/wxbridge/control.py'])
        with patch.object(build_info,'PROJECT_ROOT',self.root), \
                patch('wxbridge.release_integrity.installed_mismatches',return_value=failures):
            build_info.code_revision.cache_clear()
            try:self.assertEqual(build_info.code_revision(),'unknown')
            finally:build_info.code_revision.cache_clear()

    def test_source_only_deployment_rejects_package_and_resource_changes(self):
        check=runpy.run_path(str(ROOT/'scripts/deploy-release.py'))['require_source_only_update']
        for name in ('src/wxbridge/control.py','src/provider/client.py','config.defaults.json',
                     'src/webui/dist/index.html','requirements.txt'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'package_update_requires_upgrade_installation'):
                check([name])
        check(['scripts/wechat-login.py'])
