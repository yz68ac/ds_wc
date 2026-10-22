"""安全的运行版本标识；发布包优先使用清单，开发目录回退到 Git 提交号。"""
from functools import lru_cache
import hashlib
import json
import re
import subprocess

from wxbridge.config import PROJECT_ROOT


@lru_cache(maxsize=1)
def code_revision():
    try:
        manifest = PROJECT_ROOT/'release-manifest.json'
        if manifest.is_file():
            revision = json.loads(manifest.read_text(encoding='utf-8'))['commit']
        else:
            result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=PROJECT_ROOT,
                                    capture_output=True, text=True, timeout=3, check=True)
            revision = result.stdout.strip()
        return revision if isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision) else 'unknown'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 'unknown'


def runtime_identity(cfg):
    return dict(commit=code_revision(), config_hash=hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16])
