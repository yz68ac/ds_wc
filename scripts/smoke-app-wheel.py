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
from importlib.util import find_spec
import sys
import json
import wxbridge
from wxbridge_cli import cli
from wxbridge.paths import SOURCE_ROOT, resource_path
assert Path(wxbridge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert SOURCE_ROOT is None
assert not any(n.split('.')[0] in ('provider','wechat_linux','frida','gi','fastapi') for n in sys.modules)
renamed = {
 'wxbridge.ai.images':'wxbridge.ai.reply.images',
 'wxbridge.ai.image_generation':'wxbridge.ai.tools.image',
 'wxbridge.ai.prompts':'wxbridge.ai.reply.prompt',
 'wxbridge.ai.context_selection':'wxbridge.ai.context.selection',
 'wxbridge.ai.chat_context':'wxbridge.ai.context.chat',
 'wxbridge.ai.chat_payload':'wxbridge.ai.context.payload',
 'wxbridge.ai.identity':'wxbridge.ai.context.identity',
 'wxbridge.ai.prompt_parts':'wxbridge.ai.composer',
 'wxbridge.ai.reply_prompt':'wxbridge.ai.reply.prompt',
 'wxbridge.ai.quoted_images':'wxbridge.ai.reply.images',
 'wxbridge.ai.output':'wxbridge.ai.reply.output',
 'wxbridge.ai.citations':'wxbridge.ai.reply.citations',
 'wxbridge.ai.tool_loop':'wxbridge.ai.reply.runner',
 'wxbridge.ai.memory':'wxbridge.ai.background.memory',
 'wxbridge.ai.participation':'wxbridge.ai.background.participation',
 'wxbridge.ai.group_profile':'wxbridge.ai.background.group_profile',
 'wxbridge.ai.avatars':'wxbridge.ai.background.avatars',
 'wxbridge.ai.native_search':'wxbridge.ai.tools.native_search',
 'wxbridge.app.image_generation':'wxbridge.app.tasks.image_generation',
 'wxbridge.app.memory':'wxbridge.app.tasks.memory',
 'wxbridge.app.participation':'wxbridge.app.tasks.participation',
 'wxbridge.app.group_profile':'wxbridge.app.tasks.group_profile',
 'wxbridge.app.bot':'wxbridge.app.sessions.group',
 'wxbridge.app.account':'wxbridge.app.sessions.account',
 'wxbridge.app.coordinator':'wxbridge.app.sessions.coordinator',
 'wxbridge.app.session_phases':'wxbridge.app.sessions.phases',
 'wxbridge.app.instance_control':'wxbridge.app.sessions.instance_control',
 'wxbridge.app.reply':'wxbridge.app.tasks.reply',
 'wxbridge.app.participation_controller':'wxbridge.app.tasks.participation',
 'wxbridge.app.memory_controller':'wxbridge.app.tasks.memory',
 'wxbridge.app.group_profile_controller':'wxbridge.app.tasks.group_profile',
 'wxbridge.app.image_generation_controller':'wxbridge.app.tasks.image_generation',
 'wxbridge.app.learning':'wxbridge.app.tasks.learning',
 'wxbridge.app.task_policy':'wxbridge.app.tasks.policy',
 'wxbridge.app.model_runtime':'wxbridge.app.execution.model_runtime',
 'wxbridge.app.workers':'wxbridge.app.execution.workers',
 'wxbridge.app.locks':'wxbridge.app.execution.locks',
 'wxbridge.app.confirmation':'wxbridge.app.delivery.confirmation',
 'wxbridge.app.native_gateway':'wxbridge.app.delivery.native_gateway',
 'wxbridge.app.metrics':'wxbridge.app.status.metrics',
 'wxbridge.app.build_info':'wxbridge.app.status.build_info',
 'wxbridge.app.logging_context':'wxbridge.app.status.logging_context',
 'wxbridge.app.operations':'wxbridge.app.status.paths',
 'wxbridge.app.monitor':'wxbridge.app.status.snapshot',
 'wxbridge.app.diagnostics':'wxbridge_cli.diagnostics',
 'wxbridge.app.terminal':'wxbridge_cli.terminal',
 'wxbridge.debug_cli':'wxbridge_cli.debug',
 'wxbridge.storage.image_generation':'wxbridge.storage.image_generation_journal',
}
for old,new in renamed.items():
 package = find_spec(new.split('.')[0])
 module = Path(package.origin).parent.joinpath(*new.split('.')[1:])
 assert module.with_suffix('.py').is_file() or (module/'__init__.py').is_file(), new
 assert find_spec(old) is None, old
for name in ('reply/runner.py','tools/native_search.py','tools/registry.py','tools/base.py','tools/web.py','tools/image.py'):
 assert (Path(wxbridge.__file__).parent/'ai'/name).is_file(), name
for retired in ('wxbridge.ai.emoticons','web.backend.control','wxbridge.ai.search','wxbridge.ai.image_tools',
                'wxbridge.ai.search_prompt','wxbridge.ai.providers'):
 assert find_spec(retired) is None, retired
assert find_spec('web.backend') is not None
assert find_spec('wxbridge_web') is None
assert find_spec('wxbridge_service') is None
assert find_spec('wxbridge.cli') is None
assert find_spec('wxbridge.host.supervisor') is not None
assert find_spec('wxbridge.management.repository') is not None
assert find_spec('wxbridge.app.delivery.controller') is not None
assert not (Path(wxbridge.__file__).parent/'app/delivery.py').exists()
assert find_spec('wxbridge_cli.presentation') is not None
from wxbridge.config import InstanceConfig, default_document
assert InstanceConfig().target.context_max_chars > 0
from unittest.mock import patch
with patch.object(Path, 'read_text', side_effect=AssertionError('must not load defaults')):
    assert default_document()['target']['context_max_chars'] > 0
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
                    [str(python),'-I','-m','wxbridge_cli','debug','--help'],
                    [str(python),'-I','-m','wxbridge_cli','debug','native-api','--help'],
                    [str(python),'-I','-m','wxbridge_cli','--root',str(root),'config'],
                    [str(binary/('wxbridge.exe' if sys.platform=='win32' else 'wxbridge')),'--help'])
        for command in commands:
            subprocess.run(command,cwd=root,env=environment,check=True,stdout=subprocess.DEVNULL)
        print('OK: console script and python -m wxbridge_cli share one CLI')


if __name__ == '__main__':
    main()
