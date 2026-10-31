"""Fixed installation paths and account-scoped subprocesses; no shell evaluation."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

UNIT = 'wxbridge.service'
USER = 'wxbridge'


@dataclass(frozen=True)
class Paths:
    app: Path = Path('/opt/wxbridge')
    runtime: Path = Path('/run/wxbridge-trial')
    home: Path = Path('/var/lib/wxbridge')
    helper: Path = Path('/opt/wxbridge-native-helper')
    settings: Path = Path('/etc/wxbridge-boot.json')


def service_account():
    import pwd
    return pwd.getpwnam(USER)


def need_root():
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('此操作需要管理员权限，请使用 sudo wxbridge。')


def atomic_file(path, data, *, uid=0, gid=0, mode=0o600):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('拒绝写入符号链接路径。')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.wxbridge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if hasattr(os, 'fchown'):
                os.fchown(stream.fileno(), uid, gid)
            os.chmod(name, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, value, **kwargs):
    atomic_file(path, (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode(), **kwargs)


def user_environment(paths, account):
    return dict(PATH='/usr/local/bin:/usr/bin:/bin', HOME=account.pw_dir, LANG='C.UTF-8',
                PYTHONPATH=str(paths.app/'src'), WX_RUNTIME_DIR=str(paths.runtime))


def as_user(paths, args, *, capture=False, timeout=None):
    account = service_account()
    options = dict(cwd=paths.app, env=user_environment(paths, account), timeout=timeout)
    if capture:
        options.update(capture_output=True, text=True)
    if os.geteuid() == 0:
        options.update(user=account.pw_uid, group=account.pw_gid,
                       extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))
    elif os.geteuid() != account.pw_uid:
        raise PermissionError('此操作请使用 sudo wxbridge。')
    return subprocess.run(args, **options)


def private_json(path, *, maximum=2*1024*1024):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('状态文件不能是符号链接。')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('状态文件类型或大小无效。')
        value = json.load(stream)
        if not isinstance(value,dict):
            raise ValueError('状态文件必须是 JSON 对象。')
        return value
