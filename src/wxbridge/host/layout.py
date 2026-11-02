"""Explicit deployment layout inherited by supervised components."""
import os
from pathlib import Path


def container():
    return os.environ.get('WXBRIDGE_RUNTIME') == 'container'


def code_root():
    return Path('/opt/wxbridge')


def config_root():
    return Path('/var/lib/wxbridge-config') if container() else code_root()


def sensitive_root():
    return Path('/run/wxbridge-sensitive') if container() else Path('/run/wxbridge-trial')


def environment():
    if not container():
        return {}
    values = {'WXBRIDGE_RUNTIME': 'container'}
    for prefix in ('OPENAI','ANTHROPIC','DEEPSEEK','GEMINI'):
        for suffix in ('BASE_URL','PROXY_URL','IMAGE_BASE_URL'):
            name = prefix+'_'+suffix
            if name in os.environ:
                values[name] = os.environ[name]
    return values
