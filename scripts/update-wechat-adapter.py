"""Import a tagged local adapter release; never import its development checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def git(repository, *args):
    return subprocess.check_output(['git', *args], cwd=repository, text=True).strip()


def import_release(wheel, repository, *, root=ROOT):
    root, repository, wheel = Path(root).resolve(), Path(repository).resolve(), Path(wheel).resolve(strict=True)
    project = tomllib.loads((repository/'pyproject.toml').read_text(encoding='utf-8'))['project']
    version = project['version']
    if project['name'] != 'wechat-linux-adapter' or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('invalid_adapter_project')
    filename = f'wechat_linux_adapter-{version}-py3-none-any.whl'
    if wheel.name != filename or git(repository, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('adapter_release_requires_clean_repository')
    commit, tag = git(repository, 'rev-parse', 'HEAD'), 'v'+version
    if git(repository, 'rev-parse', tag+'^{commit}') != commit:
        raise ValueError('adapter_release_tag_mismatch')
    sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    try:
        origin = os.path.relpath(repository, root).replace('\\', '/')
    except ValueError:
        origin = repository.name
    manifest = dict(name='wechat-linux-adapter', version=version, wheel=filename, sha256=sha256,
                    source=dict(repository=origin, commit=commit, tag=tag))
    verify = runpy.run_path(str(root/'scripts/upgrade-installation.py'))['adapter_wheel']
    temporary = root/'.test-tmp'
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='adapter-import-', dir=temporary) as directory:
        candidate = Path(directory)/'dependencies'
        candidate.mkdir()
        shutil.copyfile(wheel, candidate/filename)
        (candidate/'wechat-linux-adapter.json').write_text(json.dumps(manifest), encoding='utf-8')
        verify(Path(directory))
    sources = repository/'src/wechat_linux'
    expected = {'wechat_linux/'+p.relative_to(sources).as_posix(): p.read_bytes().replace(b'\r\n', b'\n')
                for p in sources.rglob('*') if p.is_file() and p.suffix in ('.py', '.js', '.json', '.c')
                and not any(part in ('__pycache__','build') for part in p.relative_to(sources).parts)}
    with zipfile.ZipFile(wheel) as archive:
        actual = {name: archive.read(name).replace(b'\r\n', b'\n') for name in archive.namelist()
                  if name.startswith('wechat_linux/')}
    if not expected or actual != expected:
        raise ValueError('adapter_wheel_does_not_match_source')
    project_file = root/'pyproject.toml'
    text = project_file.read_text(encoding='utf-8')
    text, count = re.subn(r'"wechat-linux-adapter==[0-9]+\.[0-9]+\.[0-9]+"',
                         '"wechat-linux-adapter=='+version+'"', text)
    if count != 1:
        raise ValueError('adapter_dependency_pin_missing')
    text, count = re.subn(r'wechat-linux-adapter = \{ path = "dependencies/wechat_linux_adapter-[^"/]+\.whl" \}',
                         'wechat-linux-adapter = { path = "dependencies/'+filename+'" }', text)
    if count != 1:
        raise ValueError('adapter_dependency_source_missing')
    destination = root/'dependencies'
    destination.mkdir(exist_ok=True)
    target = destination/filename
    old_file = destination/'wechat-linux-adapter.json'
    old = json.loads(old_file.read_text(encoding='utf-8')) if old_file.exists() else None
    if target.is_symlink():
        raise ValueError('adapter_version_already_exists_with_different_content')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
        # ZIP timestamps/compression may differ across builds. Retain the pinned
        # bytes only when the existing manifest verifies and every entry agrees.
        if not old or old.get('wheel') != filename:
            raise ValueError('adapter_version_already_exists_with_different_content')
        verify(root)
        with zipfile.ZipFile(target) as existing, zipfile.ZipFile(wheel) as rebuilt:
            if (set(existing.namelist()) != set(rebuilt.namelist()) or any(
                    existing.read(name) != rebuilt.read(name) for name in existing.namelist())):
                raise ValueError('adapter_version_already_exists_with_different_content')
        manifest['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    if not target.exists():
        shutil.copyfile(wheel, target)
    old_file.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    project_file.write_text(text, encoding='utf-8')
    if old and old.get('wheel') != filename:
        old_name = old.get('wheel', '')
        if re.fullmatch(r'wechat_linux_adapter-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl', old_name):
            old_path = destination/old_name
            if (not old_path.is_symlink() and old_path.is_file()
                    and hashlib.sha256(old_path.read_bytes()).hexdigest() == old.get('sha256')):
                old_path.unlink()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--source-repo', type=Path, required=True)
    args = parser.parse_args()
    value = import_release(args.wheel, args.source_repo)
    print(f"Imported {value['name']} {value['version']} ({value['source']['commit'][:7]}).")
    print('Run python scripts/dependencies.py, then uv sync --locked --extra database --extra login --group test.')


if __name__ == '__main__':
    main()
