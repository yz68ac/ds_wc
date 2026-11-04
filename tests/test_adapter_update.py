"""A tagged SDK wheel is imported independently and tampering fails before writes."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
IMPORT = runpy.run_path(str(ROOT/'scripts/update-wechat-adapter.py'))['import_release']


class AdapterUpdateTests(unittest.TestCase):
    def test_tagged_wheel_import_and_tampered_record_are_checked(self):
        manifest=json.loads((ROOT/'dependencies/wechat-linux-adapter.json').read_text(encoding='utf-8'))
        version=manifest['version']
        wheel=ROOT/'dependencies'/manifest['wheel']
        temporary=ROOT/'.test-tmp';temporary.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary) as directory:
            base=Path(directory);repo=base/'adapter';app=base/'app'
            repo.mkdir();(app/'scripts').mkdir(parents=True)
            shutil.copyfile(ROOT/'scripts/upgrade-installation.py',app/'scripts/upgrade-installation.py')
            (repo/'pyproject.toml').write_text('[project]\nname="wechat-linux-adapter"\nversion="'+version+'"\n',encoding='utf-8')
            (app/'pyproject.toml').write_text('[project]\ndependencies=["wechat-linux-adapter==0.2.0"]\n'
                '[tool.uv.sources]\nwechat-linux-adapter = { path = "dependencies/wechat_linux_adapter-0.2.0-py3-none-any.whl" }\n',encoding='utf-8')
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if not name.startswith('wechat_linux/'):continue
                    target=repo/'src'/name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(archive.read(name))
            def git(*args):
                return subprocess.check_output(['git','-c','user.name=Synthetic','-c','user.email=synthetic@example.com',
                    '-c','commit.gpgsign=false',*args],cwd=repo,text=True).strip()
            git('init','-q');git('add','.');git('commit','-qm','synthetic');git('tag','v'+version)
            result=IMPORT(wheel,repo,root=app)
            self.assertEqual(result['source']['commit'],git('rev-parse','HEAD'))
            self.assertEqual(result['source']['tag'],'v'+version)
            self.assertIn('wechat-linux-adapter=='+version,(app/'pyproject.toml').read_text(encoding='utf-8'))
            before=(app/'dependencies/wechat-linux-adapter.json').read_bytes()
            rebuilt=base/'rebuilt'/wheel.name;rebuilt.parent.mkdir()
            with zipfile.ZipFile(wheel) as source,zipfile.ZipFile(rebuilt,'w',compression=zipfile.ZIP_STORED) as target:
                for item in source.infolist():target.writestr(item.filename,source.read(item.filename))
            self.assertNotEqual(wheel.read_bytes(),rebuilt.read_bytes())
            self.assertEqual(IMPORT(rebuilt,repo,root=app)['sha256'],result['sha256'])
            self.assertEqual((app/'dependencies/wechat-linux-adapter.json').read_bytes(),before)
            bad=base/'bad'/wheel.name;bad.parent.mkdir()
            with zipfile.ZipFile(wheel) as source,zipfile.ZipFile(bad,'w') as target:
                for item in source.infolist():
                    value=source.read(item.filename)
                    if item.filename=='wechat_linux/session.py':value+=b'\n# tampered\n'
                    target.writestr(item,value)
            with self.assertRaisesRegex(ValueError,'adapter_record_mismatch'):
                IMPORT(bad,repo,root=app)
            self.assertEqual((app/'dependencies/wechat-linux-adapter.json').read_bytes(),before)


if __name__=='__main__':
    unittest.main()
