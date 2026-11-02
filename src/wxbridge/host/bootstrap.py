"""Idempotent first-start account directory initialization."""
import os
import secrets
from pathlib import Path

def initialize_data(directory, provider_directory=None):
    """Idempotent bootstrap for Compose's short-lived init service."""
    directory = Path(directory)
    if any(p.is_symlink() for p in (directory,*directory.parents)):
        raise ValueError('invalid_container_directory')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('config','home','settings','service','catalog'):
        path=directory/name
        if path.is_symlink():raise ValueError('invalid_container_directory')
        path.mkdir(exist_ok=True,mode=0o700)
    from wxbridge.host.system import atomic_file
    password=directory/'settings/web-password'
    if not password.exists():
        atomic_file(password,(secrets.token_urlsafe(32)+'\n').encode())
    provider_root=Path(provider_directory) if provider_directory is not None else directory
    provider=provider_root/'provider.env'
    if not provider.exists():
        legacy=directory/'secrets/provider.env'
        data=b'OPENAI_API_KEY=\nANTHROPIC_API_KEY=\nDEEPSEEK_API_KEY=\nGEMINI_API_KEY=\n'
        if legacy.exists():
            import stat
            info=legacy.lstat()
            if legacy.parent.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:
                raise ValueError('invalid_legacy_provider_file')
            data=legacy.read_bytes()
        atomic_file(provider,data,gid=10001,mode=0o440)
