"""Translate bot authorization and configuration into native helper calls."""
from wechat_linux.errors import SendError
from wechat_linux.native.sender import NativeSender
from wxbridge.native.protocol import settings


def _sender(cfg, *, sending=False):
    options = settings(cfg)
    if not options['enabled']:
        raise SendError('native disabled', code='native_api_disabled', metadata={'submitted': False})
    if sending and cfg['ui']['dry_run']:
        raise SendError('native dry run', code='native_dry_run', metadata={'submitted': False})
    return NativeSender(options['helper_socket'], max_text_bytes=options['max_text_bytes']), options


def submit(cfg, chat_id, text, request_id):
    sender, options = _sender(cfg, sending=True)
    if chat_id not in options['allowed_chat_ids']:
        raise SendError('chat not allowed', code='native_chat_not_allowed', metadata={'submitted': False})
    return sender.submit_text(chat_id, text, request_id=request_id).metadata


def submit_image(cfg, chat_id, image, request_id):
    from wxbridge.app.image_generation import artifact_path
    sender, options = _sender(cfg, sending=True)
    if chat_id not in options['allowed_chat_ids']:
        raise SendError('chat not allowed', code='native_chat_not_allowed', metadata={'submitted': False})
    try:
        path = artifact_path(cfg, image)
    except Exception:
        raise SendError('image unavailable', code='native_image_unavailable', metadata={'submitted': False}) from None
    return sender.submit_image(chat_id, path, request_id=request_id).metadata


def readiness(cfg):
    sender, _ = _sender(cfg)
    return sender.health()
