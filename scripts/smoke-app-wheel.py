"""Check the installed app CLI and packaged defaults outside the source tree."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

CHECK = r'''
from pathlib import Path
import sys
import json
import wxbridge
from wxbridge import cli
from wxbridge.paths import SOURCE_ROOT, resource_path
assert Path(wxbridge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert SOURCE_ROOT is None
assert not any(n.split('.')[0] in ('provider','wechat_linux','frida','gi','fastapi') for n in sys.modules)
from wxbridge.config import DEFAULTS, DEFAULTS_FILE
assert DEFAULTS_FILE == resource_path('config.defaults.json')
assert DEFAULTS['target']['context_max_chars'] > 0
assert resource_path('prompts/system.md').read_text(encoding='utf-8').strip()
assert resource_path('prompts/memory.md').read_text(encoding='utf-8').strip()
from wxbridge.emoticons import catalog
from wxbridge.storage.memory_selection import topic_aliases
assert catalog()
assert isinstance(json.loads(resource_path('prompts/topic_aliases.json').read_text())['groups'],list)
assert isinstance(topic_aliases(),tuple)
print('OK: installed application CLI imports and packaged resources')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    wheel = parser.parse_args().wheel.resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if any(n.startswith(('provider/','wechat_linux/')) or '__pycache__' in n
               or n.endswith(('.pyc','.pyo','.env')) or '/instance/' in n for n in names):
            raise ValueError('unexpected_application_wheel_content')
        if not any(n.endswith('.data/data/share/wxbridge/config.defaults.json') for n in names):
            raise ValueError('missing_application_resources')
        if (not any(n.endswith('.data/data/share/wxbridge/webui/index.html') for n in names)
                or not any('.data/data/share/wxbridge/webui/assets/' in n for n in names)):
            raise ValueError('frontend_not_built_run_npm_build_before_packaging')
    parent = Path(__file__).resolve().parents[1]/'.test-tmp'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='app-wheel-',dir=parent) as temporary:
        root = Path(temporary)
        environment = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
        environment.update(TEMP=str(root),TMP=str(root),TMPDIR=str(root),PYTHONUTF8='1')
        subprocess.run([sys.executable,'-m','venv',str(root/'env')],env=environment,check=True)
        binary = root/'env'/('Scripts' if sys.platform=='win32' else 'bin')
        python = binary/('python.exe' if sys.platform=='win32' else 'python')
        subprocess.run([str(python),'-m','pip','install','--no-index','--no-deps',str(wheel)],
                       cwd=root,env=environment,check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(python),'-I','-B','-c',CHECK],cwd=root,env=environment,check=True)
        commands = ([str(python),'-I','-m','wxbridge','--help'],
                    [str(python),'-I','-m','wxbridge','debug','--help'],
                    [str(python),'-I','-m','wxbridge','debug','native-api','--help'],
                    [str(python),'-I','-m','wxbridge','--root',str(root),'config'],
                    [str(binary/('wxbridge.exe' if sys.platform=='win32' else 'wxbridge')),'--help'])
        for command in commands:
            subprocess.run(command,cwd=root,env=environment,check=True,stdout=subprocess.DEVNULL)
        print('OK: console script and python -m wxbridge share one CLI')


if __name__ == '__main__':
    main()
