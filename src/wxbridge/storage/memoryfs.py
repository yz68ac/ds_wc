"""验证解密副本只落在 Linux 禁止换出的 tmpfs 中。"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def is_noswap_tmpfs(path: Path, mountinfo: str) -> bool:
    best = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields, fs = before.split(), after.split()
        if not separator or len(fields) < 6 or len(fs) < 3:
            continue
        mount = Path(_unescape(fields[4]))
        if path.is_relative_to(mount) and (best is None or len(mount.parts) > best[0]):
            options = set((fields[5] + "," + fs[2]).split(","))
            best = (len(mount.parts), fs[0] == "tmpfs" and "noswap" in options)
    return best is not None and best[1]


def require_memory_directory(value: str) -> None:
    if sys.platform != "linux":
        raise ValueError("数据库试运行要求 Linux tmpfs,noswap；当前平台只做模拟验证")
    path = Path(value).resolve()
    mountinfo = Path("/proc/self/mountinfo").read_text()
    if not is_noswap_tmpfs(path, mountinfo):
        raise ValueError("decrypted_dir 必须位于 tmpfs,noswap 内存挂载中，已阻止生成磁盘明文副本")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("decrypted_dir 必须归当前用户所有，权限为 0700")
