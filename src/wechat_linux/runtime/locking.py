"""Exclusive database-owner locks shared by the bot and independent SDK sessions."""
from contextlib import contextmanager
import os
import stat
from pathlib import Path

from wechat_linux.errors import OwnerBusyError
from wechat_linux.runtime.memoryfs import require_memory_directory


@contextmanager
def instance_lock(value):
    import fcntl
    path=Path(value).absolute()
    require_memory_directory(str(path.parent))
    if path.is_symlink():raise ValueError('unsafe_runtime_lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('unsafe_runtime_lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise OwnerBusyError('database_owner_busy') from None
        yield
    finally:os.close(fd)
