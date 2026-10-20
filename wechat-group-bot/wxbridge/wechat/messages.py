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
