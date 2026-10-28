"""本机核验的 appmsg/62 拍一拍；只提取身份，不采用标题、模板或后缀。"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def _one(node, tag):
    children = [n for n in node if n.tag == tag]
    return children[0] if len(children) == 1 else None


def parse_pat(content, raw_type, chat_id):
    if (raw_type & 0xffffffff != 49 or raw_type >> 32 not in (0, 62)
            or not chat_id.endswith("@chatroom") or len(content) > 65536
            or "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper()):
        return None
    try:
        root = ET.fromstring(content.strip())
        app = _one(root, "appmsg") if root.tag == "msg" else root if root.tag == "appmsg" else None
        if app is None:
            return None
        kind, pat = _one(app, "type"), _one(app, "patinfo")
        if kind is None or (kind.text or "").strip() != "62" or pat is None:
            return None
        values = {}
        for key in ("fromusername", "pattedusername", "chatusername"):
            field = _one(pat, key)
            value = (field.text or "") if field is not None and len(field) == 0 else ""
            if not value or len(value) > 256 or any(ch.isspace() or ord(ch) < 32 for ch in value):
                return None
            values[key] = value
        if values["chatusername"] != chat_id or any(values[k].endswith("@chatroom") for k in ("fromusername", "pattedusername")):
            return None
        return dict(actor=values["fromusername"], target=values["pattedusername"], chat=chat_id)
    except (ET.ParseError, ValueError):
        return None


def is_pat(message):
    event = message.get("pat")
    return (message.get("message_kind") == "pat" and message.get("type") == 49
            and isinstance(event, dict) and bool(event.get("actor")) and bool(event.get("target"))
            and isinstance(event.get("chat"), str) and event["chat"].endswith("@chatroom")
            and event["actor"] == message.get("sender"))
