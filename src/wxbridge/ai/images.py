"""Model-facing quoted-image prompt assembly; image operations belong to wechat_linux."""
from __future__ import annotations
import time
from pathlib import Path
from wechat_linux.media import images as _media
from wechat_linux.media.images import (MediaError, V1, V2, HEX32, image_format, validate_image, decode_dat, key_candidates, read_local_image)


def _native_image(binding, cfg, account, deadline):
    from wechat_linux import NativeImageDownloader, ImageDownloadError
    from wechat_linux.native.image_contract import ERRORS
    source, intent = binding.get('download') or {}, binding.get('native_request') or {}
    options = cfg.get('native_api') or {}
    remaining = min(int(deadline-time.monotonic()), cfg['vision']['download_timeout_seconds'])
    started = time.monotonic()
    try:
        if not intent.get('request_id') or not source.get('chat_id') or not account:
            raise ImageDownloadError('native_image_source_unverified', requested=False)
        if remaining < 5:
            raise ImageDownloadError('native_image_timeout', requested=False)
        if source['chat_id'] not in options.get('allowed_chat_ids', []):
            raise ImageDownloadError('native_image_chat_not_allowed', requested=False)
        downloader = NativeImageDownloader(options['helper_socket'], account)
        image = downloader.download(binding, chat_id=source['chat_id'], request_id=intent['request_id'],
            resume_only=intent.get('resume_only', True), timeout=remaining,
            max_bytes=cfg['vision']['max_image_bytes'])
        return image.data, image.info
    except ImageDownloadError as exc:
        # Never submit a new request, switch to desktop or mislabel a thumbnail
        # as clear. A source-bound existing image remains useful to the model.
        try:
            data, info = _media.read_local_image(binding, max_bytes=cfg['vision']['max_image_bytes'])
        except MediaError:
            raise exc
        safe = exc.code if exc.code in ERRORS else 'native_image_failed'
        return data, dict(info, download=dict(backend='native', outcome=safe, requested=exc.requested,
                                             elapsed_ms=round((time.monotonic()-started)*1000)))


def prepare_multimodal(cfg, base_prompt, generation):
    """Resolve explicit images and bind quote slots without rebuilding context."""
    import base64
    import copy
    from wxbridge.ai.prompts import build_prompt
    from wxbridge.ai.prompt_parts import PromptComposer
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
            if settings.get('download_enabled', False) and settings.get('download_backend', 'desktop') == 'native':
                data, info = _native_image(binding, cfg, generation.get('account'), download_deadline)
            else:
                while True:
                    try:
                        data, info = _media.read_local_image(binding, max_bytes=settings['max_image_bytes'])
                        break
                    except MediaError as exc:
                        if str(exc) not in ("local_image_not_cached", "local_image_pending", "invalid_encrypted_image", "invalid_image") or time.monotonic() >= deadline:
                            raise
                        time.sleep(.2)
                if settings.get('download_enabled', False) and info['quality'] in ('thumbnail', 'preview'):
                    from wechat_linux.desktop.image_download import improve_image
                    data, info = improve_image(binding, cfg['ui'], data, info, download_deadline,
                        runtime_dir=str(Path(cfg['queue']['file']).parent), max_bytes=settings['max_image_bytes'])
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
    prompt = PromptComposer.references(base_prompt, triggers, account=generation.get('account', ''))
    if prompt is None:
        # Compatibility for callers supplying old plain lists or a prompt made
        # without the account map. Production build_prompt records quote slots.
        prompt = build_prompt(cfg, generation["context"], triggers[-1], generation["chat_title"], triggers,
                              system_text=base_prompt[0]["content"], memory=generation.get("memory"),
                              account=generation.get('account', ''), profile=generation.get('profile'))
    if blocks:
        text = prompt[1]["content"]
        prompt[1]["content"] = [{"type": "text", "text": text}, *blocks]
    return prompt, {"images": image_count, "references": len(metadata), "items": metadata}
