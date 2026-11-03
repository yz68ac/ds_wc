"""Model-facing quoted-image prompt assembly; image operations belong to wechat_linux."""
from __future__ import annotations
from wxbridge.config import InstanceConfig
from wxbridge.contracts.image_jobs import QuotedImageInput
import time
from pathlib import Path
from wxbridge.config import json_value
from wechat_linux.media import images as _media
from wechat_linux.media.images import (MediaError, V1, V2, HEX32, image_format, validate_image, decode_dat, key_candidates, read_local_image)


def _native_image(binding, cfg, account, deadline):
    from wechat_linux import NativeImageDownloader, ImageDownloadError
    from wechat_linux.native.image_contract import ERRORS
    source, intent = binding.get('download') or {}, binding.get('native_request') or {}
    options = cfg.account.helper_transport
    remaining = min(int(deadline-time.monotonic()), cfg.vision.download_timeout_seconds)
    started = time.monotonic()
    try:
        if not intent.get('request_id') or not source.get('chat_id') or not account:
            raise ImageDownloadError('native_image_source_unverified', requested=False)
        if remaining < 5:
            raise ImageDownloadError('native_image_timeout', requested=False)
        if source['chat_id'] not in options.allowed_chat_ids:
            raise ImageDownloadError('native_image_chat_not_allowed', requested=False)
        downloader = NativeImageDownloader(options.socket, account)
        image = downloader.download(binding, chat_id=source['chat_id'], request_id=intent['request_id'],
            resume_only=intent.get('resume_only', True), timeout=remaining,
            max_bytes=cfg.vision.max_image_bytes)
        return image.data, image.info
    except ImageDownloadError as exc:
        # Never submit a new request, switch to desktop or mislabel a thumbnail
        # as clear. A source-bound existing image remains useful to the model.
        try:
            data, info = _media.read_local_image(binding, max_bytes=cfg.vision.max_image_bytes)
        except MediaError:
            raise exc
        safe = exc.code if exc.code in ERRORS else 'native_image_failed'
        return data, dict(info, download=dict(backend='native', outcome=safe, requested=exc.requested,
                                             elapsed_ms=round((time.monotonic()-started)*1000)))


def prepare_multimodal(cfg: InstanceConfig, base_prompt, generation):
    """Read source-bound pictures, patch slots, then attach only retained media."""
    import base64
    import copy
    from wxbridge.ai.reply.prompt import build_prompt
    from wxbridge.ai.composer import PromptComposer, PromptMessages, reference_source
    from wxbridge.ai.context.selection import ContextError
    from wxbridge.storage.image_artifacts import read_artifact, ImageArtifactError
    from provider import ModelError
    triggers = copy.deepcopy(generation["triggers"])
    if getattr(base_prompt, 'references_bound', False):
        raise ContextError('图片已经绑定，取消重复替换')
    if not isinstance(base_prompt, PromptMessages) or base_prompt.layout is None:
        base_prompt = build_prompt(cfg, generation["context"], triggers[-1], generation["chat_title"], triggers,
            system_text=base_prompt[0]["content"], memory=generation.get("memory"),
            account=generation.get('account', ''), profile=generation.get('profile'))
    blocks, metadata, media, edit_images = [], [], [], {}
    image_count = 0
    settings = cfg.vision
    wait = max(0, min(float(settings.cache_wait_seconds), 10))
    deadline = time.monotonic()+wait
    download_deadline = time.monotonic()+float(settings.download_timeout_seconds)
    for binding in generation["images"]:
        targets = binding.get('targets')
        if targets is None:
            # Old queued callers/tests use a single quote slot.
            targets = [dict(trigger_index=binding['trigger_index'], depth=binding['depth'])]
        quoted = any('trigger_index' in target for target in targets)
        label = ("引用图片" if quoted else "上下文图片")+str(len(metadata)+1)
        value = dict(available=False, text='')
        try:
            if binding.get("error"):
                raise MediaError(binding["error"])
            if image_count >= int(settings.max_images):
                raise MediaError("image_count_limit")
            if binding.get('generated_image'):
                if binding.get('account') != generation.get('account'):
                    raise MediaError('image_account_mismatch')
                try:
                    _, data = read_artifact(cfg.paths.queue_file, binding['generated_image'])
                except ImageArtifactError as exc:
                    raise MediaError(exc.code) from None
                mime, width, height = validate_image(data, settings.max_image_bytes)
                info = dict(mime=mime, width=width, height=height, bytes=len(data), quality='original')
            elif settings.download_enabled and settings.download_backend == 'native':
                data, info = _native_image(binding, cfg, generation.get('account'), download_deadline)
            else:
                while True:
                    try:
                        data, info = _media.read_local_image(binding, max_bytes=settings.max_image_bytes)
                        break
                    except MediaError as exc:
                        if str(exc) not in ("local_image_not_cached", "local_image_pending", "invalid_encrypted_image", "invalid_image") or time.monotonic() >= deadline:
                            raise
                        time.sleep(.2)
                if settings.download_enabled and info['quality'] in ('thumbnail', 'preview'):
                    from wechat_linux.desktop.image_download import improve_image
                    data, info = improve_image(binding, json_value(cfg.account.ui), data, info, download_deadline,
                        runtime_dir=str(Path(cfg.paths.queue_file).parent), max_bytes=settings.max_image_bytes)
            quality = {"original": "本地原图", "cached": "本地微信缓存图（不保证原图）", "preview": "本地预览图", "thumbnail": "本地缩略图"}[info["quality"]]
            value.update(available=True, text=f"{label}随本条请求附送，来源为{quality}。请直接观察；小字不可辨时请说明，不要补造。", image_label=label,
                         image_quality=info["quality"], image_width=info["width"], image_height=info["height"])
            ids = [str(target['message_id']) for target in targets if 'message_id' in target]
            location = '聊天消息 ID '+ '、'.join(ids) if ids else '本次提问中的引用'
            blocks.append((label, [
                {"type": "text", "text": f"{label}（对应{location}；{quality}，{info['width']}×{info['height']}）："},
                {"type": "image_url", "image_url": {"url": "data:"+info["mime"]+";base64,"+base64.b64encode(data).decode("ascii"),
                    "detail": settings.detail}}]))
            image_count += 1
            metadata.append(dict(label=label, available=True, **info))
            if quoted and cfg.image_generation.enabled:
                slot = next(target for target in targets if 'trigger_index' in target)
                reference = triggers[slot['trigger_index']]['reference']
                for _ in range(slot['depth']):reference = reference['reference']
                edit_images[label] = QuotedImageInput(data, info['mime'], slot['trigger_index'], slot['depth'], reference_source(reference))
        except MediaError as exc:
            value['reason'] = f"本地图片不可读（{exc}），请说明未看到图片并请用户重新发送或打开原图后再引用。"
            metadata.append(dict(label=label, available=False, error=str(exc)))
        for target in targets:
            if 'trigger_index' in target:
                reference = triggers[target['trigger_index']]['reference']
                for _ in range(target['depth']):reference = reference['reference']
                reference.pop('reason', None)
                reference.update(value)
            else:
                media.append(dict(target, media=value))
    prompt = PromptComposer.references(base_prompt, triggers, account=generation.get('account', ''), media=media)
    retained = prompt.media_labels
    prompt.edit_images = {label: image for label, image in edit_images.items() if label in retained}
    attached = [block for label, pair in blocks if label in retained for block in pair]
    for item in metadata:
        if item['available'] and item['label'] not in retained:
            item.update(available=False, error='image_context_omitted')
    if attached:
        prompt[1]["content"] = [{"type": "text", "text": prompt[1]['content']}, *attached]
    return prompt, {"images": len(attached)//2, "references": len(metadata), "items": metadata}
