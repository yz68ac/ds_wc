"""Plan or apply a verified staged release on the existing Linux installation.

The stage is an already verified/extracted release, tested as wxbridge. Default
mode only prints a plan. Applying preserves credentials, prompts, memory and queue.
"""
from contextlib import ExitStack, closing
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import time
import runpy

APP = Path('/opt/wxbridge')
HELPER = Path('/opt/wxbridge-native-helper')
RUN = Path('/run/wxbridge-trial')
PRESERVE = {'prompts/system.md', 'prompts/memory.md', 'prompts/topic_aliases.json'}
UNITS = ('wxbridge-bot.service', 'wxbridge-recover.timer', 'wxbridge-native-helper.service')
ASSETS = ('helper.py', 'agent.js', 'profile.json')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changes(old, new):
    private_path = runpy.run_path(str(Path(__file__).with_name('check-repository.py')))['private_path']
    result = []
    for name, info in new['files'].items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                or not name or private_path(name) or info.get('mode') not in ('100644', '100755')):
            raise ValueError('invalid_release_path')
        if not (name in PRESERVE and name in old['files']) and old['files'].get(name) != info:
            result.append(name)
    removed = set(old['files'])-set(new['files'])
    if removed:
        raise ValueError('removed_files_require_review')
    return sorted(result)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], capture_output=True).returncode == 0


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=40, **kwargs)


def queue():
    with closing(sqlite3.connect((RUN/'queue.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('cursor','identity')"))
        return dict(cursor=json.loads(meta['cursor']), identity=json.loads(meta['identity']),
                    pending=db.execute("SELECT count(*) FROM tasks WHERE phase!='done'").fetchone()[0],
                    memory_pending=db.execute("SELECT count(*) FROM memory_jobs WHERE json_extract(data,'$.state')!='quarantined'").fetchone()[0])


def listener_idle(snapshot, live, *, allow_pending_memory=False):
    return (not snapshot['pending'] and (allow_pending_memory or not snapshot['memory_pending'])
            and not live.get('generation_workers', 0) and not live.get('memory', {}).get('running')
            and live.get('proactive', {}).get('outcome') not in ('evaluating','generating','generating_reply'))


def atomic(path, data, mode, uid, gid):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_install_path')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.release-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchown(stream.fileno(), uid, gid)
        os.fchmod(stream.fileno(), mode)
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary, path)


def deploy(stage, backup, *, apply=False, update_helper=False, allow_pending_memory=False):
    old = json.loads((APP/'release-manifest.json').read_text())
    new = json.loads((stage/'release-manifest.json').read_text())
    planned = changes(old, new)
    planned = [n for n in planned if n not in PRESERVE or not (APP/n).exists()]
    for name, info in new['files'].items():
        target = stage/name
        if not target.resolve().is_relative_to(stage.resolve()) or sha(target) != info['sha256']:
            raise ValueError('staged_hash_mismatch')
    helper_changed = [n for n in ASSETS if 'wxbridge/native/'+n in planned]
    report = dict(commit=new['commit'], base=old['commit'], changed_files=planned,
                  helper_assets=helper_changed, applied=False)
    if not apply:
        return report
    import fcntl
    import pwd
    user = pwd.getpwnam('wxbridge')
    if os.geteuid() != 0 or backup.exists() or backup.is_symlink():
        raise ValueError('backup_or_permissions_invalid')
    if not backup.resolve().is_relative_to(Path('/var/backups/wxbridge')):
        raise ValueError('backup_outside_private_root')
    if helper_changed and not update_helper:
        raise ValueError('helper_update_requires_explicit_flag')
    for name, info in old['files'].items():
        if name not in PRESERVE and sha(APP/name) != info['sha256']:
            raise ValueError('live_code_changed')
    if helper_changed:
        installation = json.loads((HELPER/'installation.json').read_text())
        for name in ASSETS:
            if sha(HELPER/name) != installation['files'][name]:
                raise ValueError('live_helper_changed')
    mount = run('findmnt', '-n', '-o', 'FSTYPE,OPTIONS', '--target', str(RUN)).stdout
    if mount.split()[0] != 'tmpfs' or 'noswap' not in mount:
        raise ValueError('runtime_mount_invalid')
    before_units = {u: active(u) for u in UNITS}
    if not all(before_units.values()):
        raise ValueError('services_not_ready')
    for _ in range(30):
        q = queue()
        live = json.loads((RUN/'live-status.json').read_text())
        if listener_idle(q, live, allow_pending_memory=allow_pending_memory):
            break
        time.sleep(1)
    else:
        raise ValueError('listener_not_idle')
    records = {}
    stopped = False
    mutated = False
    started = time.monotonic()
    wechat = run('pgrep', '-x', 'wechat').stdout.strip()
    try:
        stopped = True
        run('systemctl', 'stop', UNITS[1], 'wxbridge-recover.service', UNITS[0])
        if helper_changed:
            run('systemctl', 'stop', UNITS[2])
        with ExitStack() as locks:
            for path in sorted((RUN/'queue.sqlite3.lock', RUN/'decrypted/.listener.lock')):
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW);locks.callback(os.close, fd)
                if os.fstat(fd).st_uid != user.pw_uid:
                    raise ValueError('lock_owner_invalid')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = queue()
            if before['pending'] or (before['memory_pending'] and not allow_pending_memory):
                raise ValueError('queue_changed_before_switch')
            private = {n: sha(APP/n) for n in ('config.json', '.env', *sorted(PRESERVE)) if (APP/n).is_file()}
            memory = {str(p.relative_to(APP)): sha(p) for p in (APP/'memory').rglob('*') if p.is_file()}
            backup.mkdir(parents=True, mode=0o700);backup.chmod(0o700)
            updates = {APP/n: ((stage/n).read_bytes(), 0o755 if new['files'][n]['mode']=='100755' else 0o644, user.pw_uid, user.pw_gid) for n in planned}
            updates[APP/'release-manifest.json'] = ((stage/'release-manifest.json').read_bytes(), 0o644, user.pw_uid, user.pw_gid)
            if helper_changed:
                installation = json.loads((HELPER/'installation.json').read_text())
                for name in helper_changed:
                    data = (stage/'wxbridge/native'/name).read_bytes()
                    updates[HELPER/name] = (data, 0o644, 0, 0)
                    installation['files'][name] = hashlib.sha256(data).hexdigest()
                installation['review_release'] = new['commit']
                updates[HELPER/'installation.json'] = (json.dumps(installation, indent=2).encode(), 0o600, 0, 0)
            for path in updates:
                if not (path.resolve().is_relative_to(APP) or path.resolve().is_relative_to(HELPER)):
                    raise ValueError('install_path_outside_root')
                info = path.stat() if path.exists() else None
                record = dict(existed=info is not None)
                if info:
                    saved = backup/str(path).lstrip('/')
                    saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(path, saved);saved.chmod(0o600)
                    record.update(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
                records[str(path)] = record
            (backup/'restore.json').write_text(json.dumps(records));(backup/'restore.json').chmod(0o600)
            mutated = True
            for path, (data, mode, uid, gid) in updates.items():
                atomic(path, data, mode, uid, gid)
            if private != {n: sha(APP/n) for n in private} or memory != {str(p.relative_to(APP)): sha(p) for p in (APP/'memory').rglob('*') if p.is_file()} or queue() != before:
                raise ValueError('private_state_changed')
            report.update(cursor_before=before['cursor'], private_state_preserved=True,
                          pending_memory_preserved=before['memory_pending'])
        if helper_changed:
            run('systemctl', 'start', UNITS[2])
        # Readiness is a metadata-only operation, not a test message or injection.
        run('runuser', '-u', 'wxbridge', '--', '/usr/bin/python3', '-c',
            "from wxbridge.config import load_config; from wxbridge.native.sender import readiness; c=load_config('config.json'); readiness(c) if c['ui']['backend']=='native_service' else None", cwd=APP)
        stamp = time.time();run('systemctl', 'start', UNITS[0])
        for _ in range(20):
            live = json.loads((RUN/'live-status.json').read_text())
            if active(UNITS[0]) and live.get('updated_at', 0)>=stamp and live.get('build', {}).get('commit')==new['commit'] and not live.get('blocked_reason'):
                break
            time.sleep(1)
        else:
            raise ValueError('new_listener_not_ready')
        if run('pgrep', '-x', 'wechat').stdout.strip()!=wechat or queue()['identity']!=before['identity']:
            raise ValueError('client_or_identity_changed')
        report.update(applied=True, cursor_after=queue()['cursor'], phase=live.get('phase'),
                      maintenance_seconds=round(time.monotonic()-started, 3))
    except BaseException:
        if mutated:
            run('systemctl', 'stop', UNITS[0], UNITS[2], UNITS[1], 'wxbridge-recover.service')
            for name, info in records.items():
                path = Path(name)
                if info['existed']:
                    atomic(path, (backup/name.lstrip('/')).read_bytes(), info['mode'], info['uid'], info['gid'])
                elif path.exists():
                    path.unlink()
        raise
    finally:
        if stopped:
            for unit in (UNITS[2], UNITS[0], UNITS[1]):
                if before_units[unit] and not active(unit):
                    subprocess.run(['systemctl', 'start', unit], capture_output=True, timeout=40)
    report['services'] = {u: active(u) for u in UNITS}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--update-native-helper', action='store_true')
    parser.add_argument('--allow-pending-memory', action='store_true',
                        help='Preserve durable pending memory jobs; no running workers or send tasks allowed')
    args = parser.parse_args()
    try:
        print(json.dumps(deploy(args.stage, args.backup, apply=args.apply, update_helper=args.update_native_helper,
                               allow_pending_memory=args.allow_pending_memory), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps(dict(applied=False, error=type(exc).__name__, reason=str(exc) if isinstance(exc, ValueError) else 'deployment_failed')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
