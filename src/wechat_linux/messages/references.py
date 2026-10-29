"""解析微信 49/57 引用回复。只使用消息内的引用快照，不访问链接或其他群。"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _child(node, name):
    return next((n for n in node if _tag(n) == name), None) if node is not None else None


def _text(node, name):
    child = _child(node, name)
    return "".join(child.itertext()) if child is not None else ""


def _number(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _appmsg(content):
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return None
    try:
        root = ET.fromstring(content.strip())
    except (ET.ParseError, ValueError):
        return None
    return root if _tag(root) == "appmsg" else _child(root, "appmsg")


def image_md5(content):
    """Extract the image MD5 from a message snapshot."""
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return ""
    try:
        root = ET.fromstring(content.strip())
    except (ET.ParseError, ValueError):
        return ""
    for node in root.iter():
        value = node.get("md5", "") if _tag(node) == "img" else ""
        if re.fullmatch(r"[0-9a-fA-F]{32}", value):
            return value.lower()
    return ""


def local_image_cache_key(packed):
    """Linux 原图片消息 packed_info_data 的 protobuf 3→4 本地缓存名。

    只接受这个已核验字段中的 32 位十六进制标识；不扫描任意字节猜文件名。
    非完整、歧义或未知结构返回空值，不能替代原群/作者/server_id 校验。
    """
    if not isinstance(packed, (bytes, memoryview)) or len(packed) > 65536:
        return ""

    def fields(data):
        pos = 0
        def varint():
            nonlocal pos
            value = 0
            for shift in range(0, 70, 7):
                if pos >= len(data):
                    raise ValueError
                byte = data[pos]
                pos += 1
                value |= (byte & 127) << shift
                if not byte & 128:
                    return value
            raise ValueError
        result = []
        while pos < len(data):
            if len(result) >= 128:
                raise ValueError
            tag = varint()
            number, wire = tag >> 3, tag & 7
            if not 1 <= number < 2**29:
                raise ValueError
            value = None
            if wire == 0:
                varint()
            elif wire in (1, 5):
                pos += 8 if wire == 1 else 4
            elif wire == 2:
                size = varint()
                value = bytes(data[pos:pos+size])
                pos += size
            else:
                raise ValueError
            if pos > len(data):
                raise ValueError
            result.append((number, wire, value))
        return result

    try:
        outer = [value for number, wire, value in fields(packed) if number == 3 and wire == 2]
        if len(outer) != 1:
            return ""
        inner = [value for number, wire, value in fields(outer[0]) if number == 4 and wire == 2]
        if len(inner) == 1 and re.fullmatch(b"[0-9a-fA-F]{32}", inner[0]):
            return inner[0].decode("ascii").lower()
    except ValueError:
        pass
    return ""


def _reference(node, depth):
    if node is None:
        return dict(kind="missing", available=False, text="", reason="reference_missing")
    kind = _number(_text(node, "type"))
    base = kind & 0xFFFFFFFF
    from_user, chat_user = _text(node, "fromusr"), _text(node, "chatusr")
    # Linux 微信实测群引用：fromusr=群、chatusr=原作者；旧格式也可能相反。
    chat_id = next((value for value in (from_user, chat_user) if value.endswith("@chatroom")), "")
    sender = (chat_user if chat_user and not chat_user.endswith("@chatroom") else "") if from_user.endswith("@chatroom") else from_user
    content = _text(node, "content")
    result = dict(type=base, sender=sender, sender_name=_text(node, "displayname") or sender,
                  time=_number(_text(node, "createtime")) or None, server_id=_text(node, "svrid"), chat_id=chat_id)
    if base == 1:
        if sender and content.startswith(sender + ":\n"):
            content = content[len(sender)+2:]
        result.update(kind="text", available=bool(content.strip()), text=content)
        if not content.strip():
            result["reason"] = "reference_text_empty"
        return result
    if base == 3:
        field = _child(node, "content")
        if field is not None and len(field):
            content = "".join(ET.tostring(child, encoding="unicode") for child in field)
        decoded = html.unescape(content) if content.lstrip().startswith("&lt;") else content
        result.update(kind="image", available=False, text="", image_md5=image_md5(decoded),
                      reason="reference_image_not_loaded")
        return result
    if base == 49:
        field = _child(node, "content")
        if field is not None and len(field):
            content = "".join(ET.tostring(child, encoding="unicode") for child in field)
        # 仅在预期为 appmsg XML 时解开额外的一层转义，普通文字不做 HTML 转换。
        decoded = html.unescape(content) if content.lstrip().startswith("&lt;") else content
        app = _appmsg(decoded)
        if app is not None:
            subtype = _number(_text(app, "type"))
            title = _text(app, "title")
            if subtype == 57:
                result.update(kind="quoted_text", text=title, available=bool(title.strip()))
                result["reference"] = (_reference(_child(app, "refermsg"), depth+1) if depth < 3
                                       else dict(kind="unavailable", available=False, text="", reason="reference_depth_limit"))
                return result
            if subtype == 5:
                # 链接快照中的标题/摘要/URL；不声称已读取网页全文。
                snapshot = {key: _text(app, key) for key in ("title", "des", "url")}
                result.update(kind="link", available=any(snapshot.values()),
                              text="", title=snapshot['title'], description=snapshot['des'], url=snapshot['url'],
                              reason="reference_link_snapshot")
                return result
    labels = {3: "image", 34: "voice", 43: "video", 47: "sticker", 49: "attachment"}
    result.update(kind=labels.get(base, "unsupported"), available=False, text="",
                  reason="reference_content_unavailable")
    return result


def parse_quoted_reply(content: str, raw_type: int):
    """返回 (本次提问, 引用快照)，未知/损坏 appmsg 返回 None，绝不当文本触发。"""
    if raw_type & 0xFFFFFFFF != 49 or raw_type >> 32 not in (0, 57):
        return None
    app = _appmsg(content)
    if app is None or _number(_text(app, "type")) != 57:
        return None
    return _text(app, "title"), _reference(_child(app, "refermsg"), 0)
