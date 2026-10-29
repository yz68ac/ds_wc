"""Private filesystem resources without a deployment-specific mount policy."""
import os
from pathlib import Path
import stat


def require_private_directory(value):
    path = Path(value).absolute()
    if path.is_symlink():
        raise ValueError('unsafe_resource_directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('unsafe_resource_directory')
    if hasattr(os, 'geteuid') and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError('unsafe_resource_directory')
    return path
