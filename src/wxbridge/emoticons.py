"""已核实的微信文本表情与人工语义资料；无联网和数据库副作用。"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re

DATA_FILE = Path(__file__).resolve().parents[2]/'prompts/wechat_emoticons.json'


@lru_cache(maxsize=1)
def catalog():
    value = json.loads(DATA_FILE.read_text(encoding='utf-8-sig'))
    if value.get('schema_version') != 1 or not isinstance(value.get('emoticons'), list):
        raise ValueError('invalid_emoticon_catalog')
    seen = set()
    for item in value['emoticons']:
        if not item.get('meanings') or not item.get('sources'):
            raise ValueError('invalid_emoticon_entry')
        for token in [item['code'], *item['aliases']]:
            if not re.fullmatch(r'\[[^\[\]\s]{1,20}\]', token) or token in seen:
                raise ValueError('invalid_or_duplicate_emoticon_token')
            seen.add(token)
        if any(source not in value['sources'] for source in item['sources']):
            raise ValueError('unknown_emoticon_source')
    return value


@lru_cache(maxsize=1)
def tokens():
    return frozenset(token for item in catalog()['emoticons'] for token in [item['code'], *item['aliases']])


def is_token(value):
    return value in tokens()


@lru_cache(maxsize=1)
def token_pattern():
    return '(?:'+'|'.join(re.escape(token) for token in sorted(tokens(), key=len, reverse=True))+')'


@lru_cache(maxsize=1)
def semantics_prompt():
    lines = ['\n\n【微信表情语境参考】以下是本群采用的人工归纳，不是每个人固定的情绪标签。'
             '先看正文、熟悉程度和场景，不单凭一个表情认定恶意、性格、恋爱关系或修改好感度。'
             '这里仅解释常见梗义，不表示本轮必须使用表情。']
    for item in catalog()['emoticons']:
        lines.append(item['code']+'：'+'、'.join(item['meanings'])+'；避免：'+'、'.join(item['avoid']))
    return '\n'.join(lines)
