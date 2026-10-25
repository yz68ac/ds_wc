"""开机准备、只读登录/账号验证、游标检查点；日志仅输出安全状态。"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

APP = Path('/opt/wxbridge')
RUN = Path('/run/wxbridge-trial')
PERSIST = Path('/var/lib/wxbridge/recovery')
SETTINGS = Path('/etc/wxbridge-boot.json')
SCANNER = Path('/usr/local/lib/wxbridge-recovery')
BOT = 'wxbridge-bot.service'
OLD_BOT = 'wxbridge-bot-live.service'
USER = 'wxbridge'


def atomic_json(path, value, *, owner=None):
    fd, temporary = tempfile.mkstemp(prefix='.boot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if owner:
            os.chown(temporary, owner.pw_uid, owner.pw_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service_active(name):
    return subprocess.run(['systemctl', 'is-active', '--quiet', name], capture_output=True).returncode == 0


def owner():
    import pwd
    return pwd.getpwnam(USER)


def safe_status(phase, **fields):
    result = dict(phase=phase, updated_at=int(time.time()), **fields)
    if RUN.is_dir():
        atomic_json(RUN/'boot-status.json', result, owner=owner() if os.geteuid() == 0 else None)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def validate_runtime():
    account = owner()
    if RUN.is_symlink() or not RUN.is_dir():
        raise ValueError('runtime_missing_or_symlink')
    sys.path.insert(0, str(SCANNER))
    from wxbridge.storage.memoryfs import is_noswap_tmpfs
    if not is_noswap_tmpfs(RUN.resolve(), Path('/proc/self/mountinfo').read_text()):
        raise ValueError('runtime_not_private_noswap_tmpfs')
    info = RUN.stat()
    if info.st_uid != account.pw_uid or info.st_mode & 0o077:
        raise ValueError('runtime_permissions')


def runtime():
    validate_runtime()
    account = owner()
    for path in (RUN/'decrypted', PERSIST):
        if path.is_symlink():
            raise ValueError('recovery_directory_symlink')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if info.st_uid not in (0, account.pw_uid) or info.st_mode & 0o077:
            raise ValueError('recovery_directory_permissions')
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o700)
    safe_status('runtime_ready')


def checkpoint():
    path = RUN/'queue.sqlite3'
    if not path.exists():
        return dict(saved=False, reason='queue_not_created')
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        meta = {k: json.loads(v) for k,v in db.execute("SELECT key,value FROM meta WHERE key IN ('identity','cursor')")}
        pending = sum(json.loads(r[0])['phase'] != 'done' for r in db.execute('SELECT data FROM tasks'))
        memory_pending = db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0]
    finally:
        db.close()
    if not isinstance(meta.get('identity'), dict) or type(meta.get('cursor')) is not int:
        raise ValueError('checkpoint_invalid_queue_metadata')
    value = dict(schema=1, identity=meta['identity'], cursor=meta['cursor'], updated_at=int(time.time()),
                 pending_at_shutdown=pending, memory_pending_at_shutdown=memory_pending)
    atomic_json(PERSIST/'cursor.json', value)
    return dict(saved=True, cursor=value['cursor'], pending=pending, memory_pending=memory_pending)


def restored_state(checkpoint_value, identity, maximum):
    if checkpoint_value.get('schema') != 1 or checkpoint_value.get('identity') != identity:
        raise ValueError('checkpoint_account_or_group_mismatch')
    cursor = checkpoint_value.get('cursor')
    if type(cursor) is not int or cursor < 0 or cursor > maximum:
        raise ValueError('checkpoint_cursor_out_of_range')
    return dict(chats={identity['chat']: dict(last_local_id=cursor)})


def probe(keys=None, *, verify=False):
    # 只在主监听停止、协调器持有两把锁时运行；整个数据库访问留在此单进程。
    sys.path.insert(0, str(APP))
    from wxbridge.config import load_config
    from wxbridge.wechat.store import WeChatStore
    cfg = load_config(APP/'config.json')
    if keys:
        cfg['keys_file'] = str(keys)
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
        store = WeChatStore(cfg['db_dir'], cfg['keys_file'], cfg['decrypted_dir'])
        chat, title = store.resolve_chat(cfg['target']['chat'])
        identity = dict(account=store.self_wxid, chat=chat)
        if not identity['account'] or not chat.endswith('@chatroom'):
            raise ValueError('wechat_account_not_ready')
        maximum = store.last_local_id(chat)
        if cfg['ui']['backend']=='native_service':
            from wxbridge.native.sender import readiness
            readiness(cfg)
        else:
            # Desktop backends still require a discoverable target chat.
            from wxbridge.wechat.ui import A11y
            ui = A11y(cfg['ui'])
            if ui.chat_is_selected(title) is not True and ui.find_chat_item(title) is None:
                raise ValueError('wechat_login_or_target_chat_not_visible')
    cursor_file = PERSIST/'cursor.json'
    if cursor_file.exists():
        state = restored_state(json.loads(cursor_file.read_text()), identity, maximum)
        if not verify and not (RUN/'queue.sqlite3').exists():
            atomic_json(Path(cfg['state_file']), state)
    if (RUN/'queue.sqlite3').exists():
        db = sqlite3.connect((RUN/'queue.sqlite3').as_uri()+'?mode=ro', uri=True)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if row and json.loads(row[0]) != identity:
                raise ValueError('existing_queue_identity_mismatch')
        finally:
            db.close()
    return dict(ready=True, checkpoint_restored=cursor_file.exists(), latest_local_id=maximum)


def as_user(args):
    account = owner()
    def drop():
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    env = dict(PATH='/usr/local/bin:/usr/bin:/bin', HOME=account.pw_dir, PYTHONPATH=str(APP))
    allowed = {'DISPLAY','XAUTHORITY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','QT_ACCESSIBILITY',
               'QT_LINUX_ACCESSIBILITY_ALWAYS_ON','GTK_MODULES','NO_AT_BRIDGE'}
    for line in (RUN/'env').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in allowed:
            env[key] = value.strip().strip('\'"')
    return subprocess.run(args, cwd=APP, env=env, preexec_fn=drop, capture_output=True, text=True, timeout=150)


def process_token(settings):
    account = owner()
    pid = int((RUN/'wechat.pid').read_text())
    proc = Path('/proc')/str(pid)
    if proc.stat().st_uid != account.pw_uid or (proc/'exe').resolve() != Path('/opt/wechat/wechat').resolve():
        raise ValueError('wechat_process_not_ready')
    start = (proc/'stat').read_text().rsplit(')',1)[1].split()[19]
    expected = Path(settings['db_dir']).resolve()
    if not expected.is_relative_to(Path(account.pw_dir)) or expected.name != 'db_storage':
        raise ValueError('unexpected_database_path')
    opened = False
    for fd in (proc/'fd').iterdir():
        try:
            target = fd.resolve(strict=True)
            opened |= target.is_relative_to(expected) and target.suffix == '.db'
        except OSError:
            pass
    if not opened:
        raise ValueError('waiting_for_wechat_login')
    return dict(pid=pid, start=start, boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


@contextlib.contextmanager
def database_locks():
    import fcntl
    account = owner()
    with contextlib.ExitStack() as stack:
        for path in sorted({RUN/'queue.sqlite3.lock', RUN/'decrypted/.listener.lock'}):
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close,fd)
            os.fchown(fd, account.pw_uid, account.pw_gid)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def recover(*, verify=False):
    validate_runtime()
    if service_active(BOT) or service_active(OLD_BOT):
        if verify:
            raise ValueError('stop_listener_before_verification')
        result = as_user(['/usr/bin/python3', str(APP/'scripts/boot-recovery.py'), 'checkpoint'])
        if result.returncode:
            return safe_status('listening', checkpoint_error=True)
        return safe_status('listening')
    settings = json.loads(SETTINGS.read_text())
    token = process_token(settings)
    marker = RUN/'keyscan-session.json'
    cached = json.loads(marker.read_text()) if marker.exists() else None
    # 取消监听的待重启作业，避免它在密钥校验期间重新争抢数据库锁。
    subprocess.run(['systemctl','stop',BOT],check=True,capture_output=True)
    with database_locks():
        if verify or cached != token or not (RUN/'keys.json').exists():
            scratch = RUN/'root-keyscan'
            if scratch.is_symlink():
                raise ValueError('keyscan_directory_symlink')
            scratch.mkdir(mode=0o700, exist_ok=True)
            if scratch.stat().st_uid != 0 or scratch.stat().st_mode & 0o077:
                raise ValueError('keyscan_directory_permissions')
            output = scratch/'candidate.json'
            code = 'from wxbridge.wechat.keyscan import extract_keys; import sys; extract_keys(sys.argv[1],sys.argv[2],pid=int(sys.argv[3]),show_keys=False)'
            proc = subprocess.run(['/usr/bin/python3','-c',code,settings['db_dir'],str(output),str(token['pid'])],
                cwd='/', env=dict(PATH='/usr/bin:/bin', PYTHONPATH=str(SCANNER)),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            if proc.returncode or not output.exists():
                raise ValueError('key_extraction_waiting_or_failed')
            # 校验所有密钥对应文件，至少覆盖联系人、会话与主消息库。
            keys = json.loads(output.read_text())
            if not {'contact/contact.db','session/session.db','message/message_0.db'} <= set(keys):
                raise ValueError('required_database_keys_missing')
            account = owner()
            os.chown(output,account.pw_uid,account.pw_gid)
            os.chmod(output,0o600)
            destination = RUN/('verification-keys.json' if verify else 'keys.json')
            os.replace(output,destination)
            if not verify:
                atomic_json(marker,token,owner=owner())
        else:
            destination = RUN/'keys.json'
        args = ['/usr/bin/python3',str(APP/'scripts/boot-recovery.py'),'probe','--keys',str(destination)]
        if verify:
            args += ['--verify']
        result = as_user(args)
        if result.returncode:
            try:
                reason = json.loads(result.stdout).get('error','probe_failed')
            except (ValueError,TypeError):
                reason = 'probe_failed'
            raise ValueError('probe_'+reason)
        details = json.loads(result.stdout)
        if verify:
            destination.unlink(missing_ok=True)
            return safe_status('verified', key_scan=True, no_api=True, no_message_sent=True, **details)
        atomic_json(marker,token,owner=owner())
    # 必须先释放数据库锁，随后主监听才能取得同一组锁。
    subprocess.run(['systemctl','start',BOT],check=True,capture_output=True)
    return safe_status('listener_starting', **details)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode',choices=['runtime','recover','verify','probe','checkpoint','status'])
    parser.add_argument('--keys',type=Path)
    parser.add_argument('--verify',action='store_true')
    args = parser.parse_args()
    try:
        if args.mode == 'runtime':
            runtime()
        elif args.mode in ('recover','verify'):
            recover(verify=args.mode=='verify')
        elif args.mode == 'probe':
            print(json.dumps(probe(args.keys,verify=args.verify)))
        elif args.mode == 'checkpoint':
            print(json.dumps(checkpoint()))
        else:
            print((RUN/'boot-status.json').read_text() if (RUN/'boot-status.json').exists() else '{"phase":"not_started"}')
    except Exception as exc:
        # 不打印含路径/账号/密钥/聊天正文的异常内容。
        if args.mode in ('runtime','recover','verify'):
            category = str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            safe_status('waiting_for_login_or_repair', reason=category)
        else:
            import re
            reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[a-z_]{1,90}',str(exc)) else type(exc).__name__
            print(json.dumps(dict(ready=False,error=reason)))
        return 0 if args.mode == 'recover' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
