"""Runtime resource locks shared by startup and instance transitions."""
from contextlib import contextmanager
from pathlib import Path

from wechat_linux.runtime.locking import file_lock
from wxbridge.storage.memoryfs import require_memory_directory


@contextmanager
def instance_lock(value):
    require_memory_directory(str(Path(value).parent))
    with file_lock(value):
        yield
