"""Describe a caller-owned image file for native submission; no temporary copies."""
import hashlib
import os
from pathlib import Path
import stat

from wechat_linux.errors import MediaError, NativeError
from wechat_linux.media.images import validate_image
from wechat_linux.native.protocol import MAX_IMAGE_BYTES, validate_send_image


def image_request(chat_id, image_path, request_id):
    try:
        if not isinstance(image_path, (str, os.PathLike)):
            raise NativeError('invalid_image_path', submitted=False)
        path = Path(image_path).resolve(strict=True)
        if len(str(path).encode('utf-8')) > 4096:
            raise NativeError('invalid_image_path', submitted=False)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_IMAGE_BYTES:
                raise NativeError('image_size_limit', submitted=False)
            data = stream.read(MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise NativeError('image_changed', submitted=False)
        mime, _, _ = validate_image(data, MAX_IMAGE_BYTES)
        if mime not in ('image/png', 'image/jpeg'):
            raise NativeError('unsupported_image_format', submitted=False)
        return validate_send_image(dict(op='send_image', request_id=request_id, chat_id=chat_id,
            image_path=str(path), image_bytes=len(data), image_sha256=hashlib.sha256(data).hexdigest(), image_mime=mime))
    except NativeError:
        raise
    except MediaError:
        raise NativeError('invalid_image', submitted=False) from None
    except (OSError, ValueError, UnicodeError):
        raise NativeError('image_unavailable', submitted=False) from None
