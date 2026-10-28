"""Install a built SDK wheel in an isolated venv; no client, credentials or network."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


CHECK = r'''
import importlib
from importlib.resources import files
import pkgutil
from pathlib import Path
import sys
import wechat_linux
from wechat_linux import NativeOptions, NativeSender, SessionConfig, WeChatSession, capabilities
assert Path(wechat_linux.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for item in pkgutil.walk_packages(wechat_linux.__path__, 'wechat_linux.'):
    importlib.import_module(item.name)
assert not any(n.split('.')[0] in ('wxbridge', 'frida', 'gi') for n in sys.modules)
assets = files('wechat_linux.native')
for name in ('agent.js', 'profile.json', 'image_agent.js', 'image_adapter.c', 'image_profile.json'):
    assert assets.joinpath(name).read_bytes(), name
assert capabilities()['native_profiles']
assert 'bot_integrated' not in capabilities()['native_image_download']
print('OK: isolated SDK imports and native resources')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    wheel = parser.parse_args().wheel.resolve(strict=True)
    with zipfile.ZipFile(wheel) as bundle:
        if any(not (n.startswith('wechat_linux/') or '.dist-info/' in n)
               or '__pycache__' in n or n.endswith(('.pyc','.pyo')) for n in bundle.namelist()):
            raise ValueError('unexpected_wheel_content')
    parent = Path(__file__).resolve().parents[1]/'.test-tmp'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wechat-wheel-',dir=parent) as temporary:
        root = Path(temporary)
        environment = dict(os.environ, TEMP=str(root), TMP=str(root), TMPDIR=str(root))
        subprocess.run([sys.executable, '-m', 'venv', str(root/'env')], env=environment, check=True)
        python = root/'env'/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)],
                       cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([str(python), '-I', '-B', '-c', CHECK], cwd=root, env=environment, check=True)


if __name__ == '__main__':
    main()
