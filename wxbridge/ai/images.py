"""本地图片解析/内存解密；不查询数据库、不下载 URL、不落盘明文。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import time
import zlib

from wxbridge.wechat.crypto import AES

HEX32 = re.compile(r"[0-9a-fA-F]{32}\Z")
V1, V2 = b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"


class MediaError(ValueError):
    pass


def image_format(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data, max_bytes):
    if not data or len(data) > max_bytes:
        raise MediaError("image_size_limit")
    mime = image_format(data)
    width = height = None
    if mime == "image/jpeg":
        if not data.endswith(b"\xff\xd9"):
            raise MediaError("invalid_image")
        pos = 2
        while pos+3 < len(data):
            if data[pos] != 255:
                break
            while pos < len(data) and data[pos] == 255:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if pos+2 > len(data):
                break
            length = int.from_bytes(data[pos:pos+2], "big")
            if length < 2 or pos+length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height = int.from_bytes(data[pos+3:pos+5], "big")
                width = int.from_bytes(data[pos+5:pos+7], "big")
                break
            pos += length
        if width is None:
            raise MediaError("invalid_image")
    elif mime == "image/png":
        if len(data) < 33:
            raise MediaError("invalid_image")
        if data[12:16] != b"IHDR" or int.from_bytes(data[8:12], "big") != 13:
            raise MediaError("invalid_image")
        width, height = struct.unpack_from(">II", data, 16)
        pos, ended = 8, False
        while pos+12 <= len(data):
            length = int.from_bytes(data[pos:pos+4], "big")
            end = pos+12+length
            if end > len(data) or zlib.crc32(data[pos+4:end-4]) & 0xFFFFFFFF != int.from_bytes(data[end-4:end], "big"):
                raise MediaError("invalid_image")
            if data[pos+4:pos+8] == b"IEND":
                ended = end == len(data)
                break
            pos = end
        if not ended:
            raise MediaError("invalid_image")
    elif mime == "image/gif":
        if len(data) < 14 or data[-1] != 0x3B:
            raise MediaError("invalid_image")
        width, height = struct.unpack_from("<HH", data, 6)
    elif mime == "image/webp":
        if len(data) < 30 or int.from_bytes(data[4:8], "little")+8 != len(data):
            raise MediaError("invalid_image")
        if data[12:16] == b"VP8X":
            width, height = int.from_bytes(data[24:27], "little")+1, int.from_bytes(data[27:30], "little")+1
        elif data[12:16] == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif data[12:16] == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF)+1, ((bits >> 14) & 0x3FFF)+1
        else:
            raise MediaError("unsupported_image_format")
    else:
        raise MediaError("unsupported_image_format")
    if not width or not height or max(width, height) > 8192:
        raise MediaError("image_dimension_limit")
    return mime, width, height


def decode_dat(data, key=None, xor_key=None):
    if image_format(data) or data.startswith(b"wxgf"):
        return data
    if data[:6] in (V1, V2):
        if len(data) < 31 or AES is None:
            raise MediaError("invalid_encrypted_image")
        aes_len, xor_len = struct.unpack_from("<II", data, 6)
        aligned = (aes_len//16+1)*16
        end = 15+aligned
        if aes_len <= 0 or end > len(data) or xor_len > len(data)-end:
            raise MediaError("invalid_encrypted_image")
        if data[:6] == V1:
            key = b"cfcd208495d565ef"
        if not key or len(key) != 16:
            raise MediaError("image_key_unavailable")
        plain = AES.new(key, AES.MODE_ECB).decrypt(data[15:end])
        padding = plain[-1]
        if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding])*padding or len(plain)-padding != aes_len:
            raise MediaError("image_key_invalid")
        prefix = plain[:-padding]
        tail_start = len(data)-xor_len
        if xor_key is None and xor_len:
            if prefix.startswith(b"\xff\xd8\xff") and xor_len >= 2 and data[-1]^0xD9 == data[-2]^0xFF:
                xor_key = data[-1]^0xD9
            elif prefix.startswith(b"\x89PNG") and xor_len >= 12:
                expected = b"\x00\x00\x00\x00IEND\xaeB`\x82"
                keys = {a ^ b for a,b in zip(data[-12:], expected)}
                if len(keys) == 1:
                    xor_key = keys.pop()
        if xor_len and xor_key is None:
            raise MediaError("image_key_unavailable")
        tail = bytes(value ^ (xor_key or 0) for value in data[tail_start:])
        return prefix + data[end:tail_start] + tail
    if data:
        for first in (0xFF, 0x89, 0x47, 0x52, 0x77):
            xor = data[0] ^ first
            header = bytes(value ^ xor for value in data[:16])
            if image_format(header) or header.startswith(b"wxgf"):
                return bytes(value ^ xor for value in data)
    raise MediaError("unsupported_image_format")


def key_candidates(account, account_root, home):
    """Linux 实测：本账号 kvcomm 缓存名中的 uin 与账号名推导；不扫描内存。"""
    uids = set()
    for folder in (Path(home)/".xwechat/net/kvcomm", Path(home)/".xwechat/ilink/kvcomm"):
        for p in folder.glob("key_*.statistic"):
            match = re.match(r"key_(\d{1,20})_", p.name)
            if match:
                uids.add(match.group(1))
            if len(uids) >= 16:
                break
    for uid in sorted(uids):
        for name in dict.fromkeys((account, Path(account_root).name)):
            yield hashlib.md5((uid+name).encode()).hexdigest()[:16].encode("ascii"), int(uid)&255


def read_local_image(binding, cfg):
    settings = cfg.get("vision") or {}
    max_bytes = int(settings.get("max_image_bytes", 8*1024*1024))
    root = Path(binding["account_root"]).resolve()
    last_error = binding.get("error") or "local_image_not_cached"
    error_rank = 0
    failures = []
    def remember(error, rank):
        nonlocal last_error, error_rank
        if rank >= error_rank:
            last_error, error_rank = error, rank
    candidates = binding.get("candidates") or []
    for candidate in candidates:
        path = root/candidate["relative"]
        try:
            resolved = path.resolve()
            if (not resolved.is_relative_to(root)
                    or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root))
                    or not any(resolved.is_relative_to(root/part) for part in ("msg/attach", "cache"))):
                raise MediaError("unsafe_image_path")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes+1024:
                    raise MediaError("image_size_limit")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    encrypted = stream.read(max_bytes+1025)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise MediaError("local_image_pending")
            finally:
                os.close(fd)
            keys = list(key_candidates(binding["account"], root, binding["home"])) if encrypted[:6] == V2 else [(None, None)]
            if not keys:
                raise MediaError("image_key_unavailable")
            for key, xor in keys:
                try:
                    image = decode_dat(encrypted, key, xor)
                except MediaError as exc:
                    remember(str(exc), 1)
                    continue
                source_format = "wxgf" if image.startswith(b"wxgf") else None
                try:
                    if source_format:
                        from wxbridge.ai.wxgf import WxgfError, decode_wxgf
                        try:
                            image = decode_wxgf(image, max_bytes)
                        except WxgfError as exc:
                            raise MediaError(str(exc)) from None
                    mime, width, height = validate_image(image, max_bytes)
                    info = dict(mime=mime, width=width, height=height, bytes=len(image), quality=candidate["quality"])
                    if candidate.get('binding_source') == 'verified_message_cache':
                        info["binding_source"] = "verified_message_cache"
                    if binding.get('index_error'):
                        info['index_error'] = binding['index_error']
                    if source_format:
                        info.update(source_format=source_format, converted=True)
                    if failures:
                        info["fallback_errors"] = failures
                    return image, info
                except MediaError as exc:
                    remember(str(exc), 3 if source_format or image_format(image) else 2)
                    if source_format:
                        failures.append(dict(quality=candidate["quality"], error=str(exc)))
                        # 已确认 wxgf 容器并尝试转码，不再让错误密钥覆盖实际失败原因。
                        break
        except FileNotFoundError:
            continue
        except (OSError, MediaError) as exc:
            remember(str(exc) if isinstance(exc, MediaError) else "local_image_read_failed", 2)
    raise MediaError(last_error)


def prepare_multimodal(cfg, base_prompt, generation):
    """工作进程内取图并重建提示词；只传明确引用图片，不含历史其他图片。"""
    import base64
    import copy
    from wxbridge.ai.prompts import build_prompt
    triggers = copy.deepcopy(generation["triggers"])
    blocks, metadata = [], []
    image_count = 0
    settings = cfg.get("vision") or {}
    wait = max(0, min(float(settings.get("cache_wait_seconds", 3)), 10))
    deadline = time.monotonic()+wait
    download_deadline = time.monotonic()+float(settings.get('download_timeout_seconds', 20))
    for binding in generation["images"]:
        reference = triggers[binding["trigger_index"]]["reference"]
        for _ in range(binding["depth"]):
            reference = reference["reference"]
        reference.pop("reason", None)
        label = "引用图片" + str(len(metadata)+1)
        try:
            if binding.get("error"):
                raise MediaError(binding["error"])
            if image_count >= int(settings.get("max_images", 4)):
                raise MediaError("image_count_limit")
            while True:
                try:
                    data, info = read_local_image(binding, cfg)
                    break
                except MediaError as exc:
                    if str(exc) not in ("local_image_not_cached", "local_image_pending", "invalid_encrypted_image", "invalid_image") or time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            if settings.get('download_enabled', False) and info['quality'] in ('thumbnail', 'preview'):
                from wxbridge.wechat.image_download import improve_image
                data, info = improve_image(binding, cfg, data, info, download_deadline)
            quality = {"original": "本地原图", "cached": "本地微信缓存图（不保证原图）", "preview": "本地预览图", "thumbnail": "本地缩略图"}[info["quality"]]
            reference.update(available=True, text=f"{label}随本条请求附送，来源为{quality}。请直接观察；小字不可辨时请说明，不要补造。", image_label=label,
                             image_quality=info["quality"], image_width=info["width"], image_height=info["height"])
            blocks.append({"type": "text", "text": f"{label}（对应上面本次提问中的引用；{quality}，{info['width']}×{info['height']}）："})
            blocks.append({"type": "image_url", "image_url": {"url": "data:"+info["mime"]+";base64,"+base64.b64encode(data).decode("ascii"),
                                                                 "detail": settings.get("detail", "original")}})
            image_count += 1
            metadata.append(dict(label=label, available=True, **info))
        except MediaError as exc:
            reference.update(available=False, text="", reason=f"本地图片不可读（{exc}），请说明未看到图片并请用户重新发送或打开原图后再引用。")
            metadata.append(dict(label=label, available=False, error=str(exc)))
    prompt = build_prompt(cfg, generation["context"], triggers[-1], generation["chat_title"], triggers,
                          system_text=base_prompt[0]["content"], memory=generation.get("memory"),
                          account=generation.get('account', ''))
    if blocks:
        text = prompt[1]["content"]
        prompt[1]["content"] = [{"type": "text", "text": text}, *blocks]
    return prompt, {"images": image_count, "references": len(metadata), "items": metadata}
