"""Locks for caller-selected filesystem resources."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

from wechat_linux.errors import OwnerBusyError
from wechat_linux.runtime.paths import require_private_directory


@contextmanager
def file_lock(value, *, blocking=False):
    import fcntl
    path = Path(value).absolute()
    require_private_directory(path.parent)
    if path.is_symlink():
        raise ValueError('unsafe_resource_lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_resource_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise OwnerBusyError('resource_busy') from None
        yield
    finally:
        os.close(fd)
