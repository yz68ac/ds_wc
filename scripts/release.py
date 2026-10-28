"""从已提交代码构建可核对的发布包；不包含运行数据，不连接服务器。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'release-manifest.json'
LIMIT = 32*1024*1024


def rules():
    values = runpy.run_path(str(ROOT/'scripts'/'check-repository.py'))
    return values['private_path'], values['credential_lines']


def valid_path(name):
    parts = PurePosixPath(name).parts
    return (isinstance(name, str) and bool(parts) and not name.startswith('/') and '\\' not in name
            and ':' not in name and all(p not in ('.', '..', '') for p in name.split('/')))


def verify(path):
    private, credentials = rules()
    with zipfile.ZipFile(path) as bundle:
        infos = bundle.infolist()
        names = [i.filename for i in infos]
        if (len(names) != len(set(names)) or len(names) > 1000 or MANIFEST not in names
                or sum(i.file_size for i in infos) > LIMIT):
            raise ValueError('invalid_release_inventory')
        for info in infos:
            if not valid_path(info.filename) or info.is_dir() or (info.external_attr >> 16) & 0o170000 not in (0, 0o100000):
                raise ValueError('invalid_release_path')
        manifest = json.loads(bundle.read(MANIFEST))
        if (not isinstance(manifest, dict) or set(manifest) != {'format', 'commit', 'files'} or manifest.get('format') != 1
                or not isinstance(manifest.get('commit'), str) or not re.fullmatch('[a-f0-9]{40}', manifest['commit'])
                or not isinstance(manifest.get('files'), dict) or set(names)-{MANIFEST} != set(manifest['files'])):
            raise ValueError('invalid_release_manifest')
        for name, expected in manifest['files'].items():
            if not isinstance(expected, dict) or expected.get('mode') not in ('100644', '100755'):
                raise ValueError('invalid_release_mode')
            body = bundle.read(name)
            if private(name) or credentials(body):
                raise ValueError('private_release_content')
            if hashlib.sha256(body).hexdigest() != expected.get('sha256'):
                raise ValueError('release_hash_mismatch')
        required = {'config.defaults.json', 'src/wxbridge/__main__.py', 'src/provider/__init__.py',
                    'src/wechat_linux/__init__.py', 'scripts/test-offline.py', 'prompts/system.md'}
        if not required.issubset(manifest['files']):
            raise ValueError('incomplete_release')
        return manifest


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout


def build(path, root=ROOT):
    if git(root, 'status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('commit_changes_before_build')
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    source = git(root, 'archive', '--format=zip', 'HEAD')
    modes = {}
    for item in git(root, 'ls-tree', '-rz', 'HEAD').split(b'\0'):
        if item:
            metadata, name = item.split(b'\t', 1)
            modes[name.decode()] = metadata.split()[0].decode()
    output = io.BytesIO()
    manifest = dict(format=1, commit=commit, files={})
    with zipfile.ZipFile(io.BytesIO(source)) as archived, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for info in archived.infolist():
            if info.is_dir():
                continue
            body = archived.read(info.filename)
            bundle.writestr(info, body)
            manifest['files'][info.filename] = dict(sha256=hashlib.sha256(body).hexdigest(), mode=modes[info.filename])
        bundle.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    raw = output.getvalue()
    verify(io.BytesIO(raw))
    # 独占创建，拒绝覆盖已有发布包；输出放仓库外，避免下次污染工作区。
    with open(path, 'xb') as stream:
        stream.write(raw)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify'))
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        result = build(args.file) if args.action == 'build' else verify(args.file)
        print(json.dumps(dict(commit=result['commit'], files=len(result['files']), verified=True)))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        # 不打印发布文件正文、凭据命中或 Git stderr。
        print('Release failed: '+type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
