"""Reentrant, process-shared locks for file-backed management operations."""
import os
from pathlib import Path
import stat
import threading
import time

_registry = {}
_guard = threading.Lock()


class FileLock:
    def __new__(cls, path):
        key = os.path.normcase(str(Path(path).absolute()))
        with _guard:
            if key not in _registry:
                value = super().__new__(cls)
                value.path, value.mutex, value.depth, value.fd = Path(path), threading.RLock(), 0, None
                _registry[key] = value
            return _registry[key]

    def __enter__(self):
        self.mutex.acquire()
        try:
            if self.depth == 0:
                if self.path.is_symlink():
                    raise ValueError('unsafe_management_lock')
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0), 0o600)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and
                            (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
                        raise ValueError('unsafe_management_lock')
                    if os.name == 'posix':
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    else:
                        import msvcrt
                        if not info.st_size:
                            os.write(fd, b'0')
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                os.lseek(fd, 0, os.SEEK_SET)
                                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                                break
                            except OSError:
                                if time.monotonic() >= deadline:
                                    raise TimeoutError('management_lock_timeout') from None
                                time.sleep(.05)
                    self.fd = fd
                except BaseException:
                    os.close(fd)
                    raise
            self.depth += 1
            return self
        except BaseException:
            self.mutex.release()
            raise

    def __exit__(self, *exc):
        self.depth -= 1
        try:
            if self.depth == 0:
                try:
                    if os.name != 'posix':
                        import msvcrt
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(self.fd)
                    self.fd = None
        finally:
            self.mutex.release()
