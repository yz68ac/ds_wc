"""Build the three release wheels from a fresh public-source snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def stage_source(source, target):
    private = runpy.run_path(str(ROOT/'scripts/check-repository.py'))['private_path']
    manifest = source/'release-manifest.json'
    if manifest.is_file():
        entries = json.loads(manifest.read_text())['files']
    else:
        names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=source)
        entries = {name:None for name in names.decode().split('\0') if name}
    for name, info in entries.items():
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or private(name)
                or any(p in ('build', 'dist', '__pycache__', 'node_modules') or p.endswith('.egg-info')
                       for p in path.parts) and not name.startswith('src/webui/dist/')):
            raise ValueError('unsafe_build_source')
        src = source/name
        if src.is_symlink() or not src.resolve().is_relative_to(source.resolve()):
            raise ValueError('unsafe_build_source')
        if info and hashlib.sha256(src.read_bytes()).hexdigest() != info['sha256']:
            raise ValueError('build_source_hash_mismatch')
        dst = target/name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for src in (source/'src/webui/dist').rglob('*'):
        if src.is_file():
            if src.is_symlink() or not src.resolve().is_relative_to(source.resolve()):
                raise ValueError('unsafe_frontend_file')
            name = src.relative_to(source)
            if manifest.is_file() and name.as_posix() not in entries:
                raise ValueError('unverified_frontend_file')
            dst = target/name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def build(source, output, *, offline=False):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob('*.whl')):
        raise ValueError('wheel_output_not_empty')
    temporary = ROOT/'.test-tmp'
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wheel-source-', dir=temporary) as directory:
        stage = Path(directory)
        stage_source(source, stage)
        for relative in ('.', 'src/provider', 'src/wechat_linux'):
            subprocess.run(['uv', 'build', str(stage/relative), '--wheel', '--python', sys.executable,
                            '--out-dir', str(output), *(['--offline'] if offline else [])], check=True)
        verify = runpy.run_path(str(ROOT/'scripts/upgrade-installation.py'))['verify_wheels']
        verify(stage, output)
    return sorted(output.glob('*.whl'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    for wheel in build(args.source, args.out, offline=args.offline):
        print(wheel)


if __name__ == '__main__':
    main()
