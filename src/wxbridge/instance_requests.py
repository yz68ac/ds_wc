"""Revision-bound instance requests shared by Web and the owner loop."""
import json
import re

REQUEST_FILE = '.runtime-request.json'


def read_request(path):
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 4096:
        raise ValueError('invalid_instance_request')
    value=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value,dict) or set(value)!={'id','action','revision','created_at'}
            or not isinstance(value['id'],str) or not re.fullmatch('[a-f0-9]{32}',value['id'])
            or value['action'] not in {'start','stop','apply'}
            or not isinstance(value['revision'],str) or not re.fullmatch('[a-f0-9]{64}',value['revision'])
            or type(value['created_at']) not in (int,float)):
        raise ValueError('invalid_instance_request')
    return value
