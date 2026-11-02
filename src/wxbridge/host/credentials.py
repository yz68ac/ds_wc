"""Validate direct, read-only Provider files without copying credentials."""
import os
from pathlib import Path
import stat

from .system import service_account


def validate_provider(source):
    source = Path(source)
    if source.parent != Path('/run/secrets/wxbridge') or source.is_symlink():
        raise ValueError('unsafe_provider_secret_source')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(source, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024*1024:
            raise ValueError('invalid_provider_secret_source')
    account = service_account()
    readable = ((info.st_uid == account.pw_uid and info.st_mode & stat.S_IRUSR)
                or (info.st_gid == account.pw_gid and info.st_mode & stat.S_IRGRP))
    parent = source.parent.stat()
    traversable = ((parent.st_uid == account.pw_uid and parent.st_mode & stat.S_IXUSR)
                   or (parent.st_gid == account.pw_gid and parent.st_mode & stat.S_IXGRP)
                   or parent.st_mode & stat.S_IXOTH)
    if info.st_mode & 0o007 or not readable or not traversable:
        raise ValueError('provider.env must be private and readable by uid/gid 10001; use root:10001 mode 0440 and directory 0750')
    return source
