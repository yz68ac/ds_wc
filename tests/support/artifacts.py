"""Copy the pinned adapter artifact into isolated packaging/upgrade fixtures."""
import json
from pathlib import Path
import shutil
from tests.support.paths import ROOT


def adapter_artifact(target):
    source = ROOT/'dependencies'
    manifest = json.loads((source/'wechat-linux-adapter.json').read_text(encoding='utf-8'))
    directory = Path(target)/'dependencies'
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source/'wechat-linux-adapter.json', directory/'wechat-linux-adapter.json')
    shutil.copyfile(source/manifest['wheel'], directory/manifest['wheel'])
    return directory/manifest['wheel']
