"""发布包只取已提交代码，校验篡改、私密文件与路径穿越。"""
import io
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
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
        for name in ('config.defaults.json', 'wxbridge/__main__.py', 'scripts/test-offline.py', 'prompts/system.md'):
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
        data = self.altered(lambda files: files.update({'wxbridge/__main__.py': b'tampered'}))
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            release['verify'](data)

    def test_traversal_archive_is_rejected_before_extracting(self):
        data = self.altered(lambda files: files.update({'../outside': b'synthetic'}))
        with self.assertRaisesRegex(ValueError, 'invalid_release_path'):
            release['verify'](data)
