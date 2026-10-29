"""消息文本处理：触发兼容、前缀处理和回复分片。

真实 @ 判定仍由 bot.py 根据数据库元数据完成；这里的文本触发仅供兼容模式。
"""
from __future__ import annotations

import re
from typing import Any


def _strip_prefix(text: str, prefixes: list[str]) -> str:
    cleaned = text.lstrip()
    for prefix in prefixes:
        if not prefix:
            continue
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].lstrip(" ：:，,、\t")
            break
    return cleaned


def is_triggered(cfg: dict[str, Any], text: str) -> bool:
    trigger = cfg["target"]["trigger"]
    text = (text or "").strip()
    if not text:
        return False
    if trigger.get("reply_all"):
        return True
    for mention in trigger.get("mentions") or []:
        if mention and re.search(re.escape(mention) + r"(?=$|[\s:：,，。.!！?？;；])", text, re.I):
            return True
    prefixes = trigger.get("prefixes") or []
    return _strip_prefix(text, prefixes) != text


def chunk_text(text: str, limit: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current.strip():
            chunks.append(current.strip())
        current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current.strip():
        chunks.append(current.strip())
    return chunks


def reply_chunks(text: str, limit: int) -> list[str]:
    """模型明确写成两段时按两条发送；完整保留内容，超长仍走安全分片。"""
    parts = re.split(r"\n[ \t]*\n", text.strip())
    # 保留代码里的空行与连续块；代码仍只有超过传输上限才分片。
    code = any(re.match(r"(?:[ \t]+\S|def |class |import |from \w+.* import |[{}]\s*$|```|#|SELECT\b|CREATE\b)", line)
               for line in text.splitlines())
    if not code and len(parts) == 2 and len(text.strip()) > 30 and all(p.strip() for p in parts):
        return [chunk for part in parts for chunk in chunk_text(part, limit)]
    return chunk_text(text, limit)


MSG_TYPE_LABEL = {
    3: '[图片]', 34: '[语音]', 42: '[名片]', 43: '[视频]', 47: '[表情]',
    48: '[位置]', 49: '[链接/文件]', 50: '[通话]', 10000: '[系统消息]',
}
REFERENCE_REASONS = {
    'reference_missing': '引用记录缺失',
    'reference_text_empty': '引用文字为空',
    'reference_image_not_loaded': '尚未读取被引用的本地图片',
    'reference_depth_limit': '更深层引用未展开',
    'reference_link_snapshot': '仅有链接标题、摘要和地址，未抓取网页正文',
    'reference_content_unavailable': '引用快照不含可读正文；未下载媒体或执行识别',
}


def display_reference(reference):
    result = dict(reference)
    result['sender_name'] = result.get('sender_name') or result.get('sender') or '未知作者'
    if result.get('reason') in REFERENCE_REASONS:
        result['reason'] = REFERENCE_REASONS[result['reason']]
    if result.get('kind') == 'link' and any(result.get(key) for key in ('title', 'description', 'url')):
        result['text'] = '\n'.join(result.get(key, '') for key in ('title', 'description', 'url') if result.get(key))
    if isinstance(result.get('reference'), dict):
        result['reference'] = display_reference(result['reference'])
    return result


def display_message(message):
    result = dict(message)
    if result.get('message_kind') == 'quoted_text':
        result['type'] = 1
    elif result.get('message_kind') == 'pat':
        result['text'] = '[拍一拍]'
    elif result.get('type') == 3:
        result['text'] = '[图片]'
    elif result.get('type') != 1 and not result.get('text'):
        result['text'] = MSG_TYPE_LABEL.get(result.get('type'), f"[type={result.get('type', 0)}]")
    if isinstance(result.get('reference'), dict):
        result['reference'] = display_reference(result['reference'])
    return result
