"""Container-only process cleanup, guarded by an init parent and private /proc."""
import os
from pathlib import Path
import signal


def validate_namespace():
    init = Path('/proc/1/exe').resolve().name
    if init not in ('docker-init', 'tini') or os.getppid() != 1:
        raise ValueError('container_requires_private_pid_namespace_and_init')
    if not hasattr(os, 'pidfd_open'):
        raise ValueError('container_requires_pidfd')


def remaining_processes():
    validate_namespace()
    result = []
    for item in Path('/proc').iterdir():
        if not item.name.isdigit() or int(item.name) in (1, os.getpid()):
            continue
        try:
            if (item/'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                result.append(int(item.name))
        except FileNotFoundError:
            pass
    return result


def signal_pid(pid, sig):
    if pid in (1, os.getpid()):
        raise ValueError('refusing_protected_process')
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def wechat_processes(uid):
    result = []
    for pid in remaining_processes():
        proc = Path('/proc')/str(pid)
        try:
            if proc.stat().st_uid == uid and (proc/'exe').resolve() == Path('/opt/wechat/wechat'):
                result.append(pid)
        except FileNotFoundError:
            pass
    return result
