"""Plan or apply a verified staged release on the existing Linux installation.

The stage is an already verified/extracted release, tested as wxbridge. Default
mode only prints a plan. Applying preserves credentials, prompts, memory and queue.
"""
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import time
import runpy
import sys

APP = Path('/opt/wxbridge')
HELPER = Path('/opt/wxbridge-native-helper')
RUN = Path('/run/wxbridge-trial')
PRESERVE = {'prompts/system.md', 'prompts/memory.md', 'prompts/topic_aliases.json'}
UNITS = ('wxbridge.service',)
ASSETS = ('helper.py', 'agent.js', 'profile.json')
IMAGE_ASSETS = ('image_worker.py', 'image_contract.py', 'image_agent.js', 'image_adapter.c', 'image_profile.json')
HELPER_CONFIG = HELPER/'helper-config.json'
HELPER_DROPIN = Path('/etc/systemd/system/wxbridge.service.d/native-images.conf')
IMAGE_STOP_POLICY = (b'[Service]\nTimeoutStopSec=infinity\nSendSIGKILL=no\n'
                     b'RuntimeDirectory=wechat-linux-images\nRuntimeDirectoryMode=0700\n'
                     b'RuntimeDirectoryPreserve=yes\nReadWritePaths=/run/wechat-linux-images\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changes(old, new, *, allow_removed=(), update_prompts=False):
    private_path = runpy.run_path(str(Path(__file__).with_name('check-repository.py')))['private_path']
    result = []
    for name, info in new['files'].items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                or not name or private_path(name) or info.get('mode') not in ('100644', '100755')):
            raise ValueError('invalid_release_path')
        if not (name in PRESERVE and name in old['files'] and not update_prompts) and old['files'].get(name) != info:
            result.append(name)
    removed = set(old['files'])-set(new['files'])
    if removed != set(allow_removed) or removed & PRESERVE:
        raise ValueError('removed_files_require_review')
    return sorted(result)


def helper_asset(manifest, name):
    for prefix in ('src/wechat_linux/native/', 'wechat_linux/native/', 'wxbridge/native/'):
        if prefix+name in manifest['files']:
            return prefix+name
    raise ValueError('helper_asset_missing')


def helper_changes(old, new):
    return [name for name in ASSETS if old['files'][helper_asset(old, name)]['sha256']
            != new['files'][helper_asset(new, name)]['sha256']]


def image_configuration(config, *, enable=False, account=None, account_root=None):
    """Require an explicit account binding before enabling native downloads."""
    result = json.loads(json.dumps(config))
    if not enable:
        if account is not None or account_root is not None:
            raise ValueError('image_options_require_enable')
        return result
    options = runpy.run_path(str(Path(__file__).with_name('install-native-helper.py')))['image_options']
    image = options(True, account, account_root, result['uid'])
    previous = result.get('image_download')
    if previous and previous.get('enabled') and previous != image:
        raise ValueError('image_account_change_requires_review')
    result['image_download'] = image
    return result


def image_helper_changes(new, installation):
    """The app manifest alone cannot prove optional assets were installed."""
    files = installation.get('files', {})
    return [name for name in IMAGE_ASSETS
            if files.get(name) != new['files'][helper_asset(new, name)]['sha256']]


def stop_helper(*, images_enabled):
    # A Python timeout must not let rollback overwrite assets while native C
    # callbacks are still owned by WeChat. Never fall back to kill/restart.
    if images_enabled and active(UNITS[0]):
        policy = run('systemctl', 'show', UNITS[0], '-p', 'TimeoutStopUSec', '-p', 'SendSIGKILL').stdout
        values = dict(line.split('=', 1) for line in policy.splitlines() if '=' in line)
        if values.get('TimeoutStopUSec') != 'infinity' or values.get('SendSIGKILL') != 'no':
            raise ValueError('native_image_stop_policy_unsafe')
    return subprocess.run(['systemctl', 'stop', UNITS[0]], capture_output=True,
                          text=True, check=True, timeout=None if images_enabled else 40)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], capture_output=True).returncode == 0


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=40, **kwargs)


def require_application_layout():
    """Reject pre-package installations before stopping or replacing live files."""
    try:
        run(str(APP/'.venv/bin/python'),'-I','-c',
            "from importlib.metadata import distribution; distribution('wxbridge')")
    except (OSError,subprocess.CalledProcessError):
        raise ValueError('application_environment_upgrade_required') from None
    command = run('systemctl','show',UNITS[0],'-p','ExecStart','--value').stdout
    if '-m wxbridge_service' not in command:
        raise ValueError('service_entry_upgrade_required')


def wait_native_ready():
    """Type=simple becomes active before the helper binds its private socket."""
    deadline = time.monotonic()+10
    while True:
        try:
            return run('runuser', '-u', 'wxbridge', '--', '/usr/bin/env', 'PYTHONPATH='+str(APP/'src'), '/usr/bin/python3', '-c',
                "from wxbridge.config import load_config; from wxbridge.native.sender import readiness; c=load_config('config.json'); readiness(c) if c['ui']['backend']=='native_service' else None", cwd=APP, env={**os.environ, 'PYTHONPATH': str(APP/'src')})
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def configured_groups():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from wxbridge.app.operations import configured_groups as groups
    return groups(APP, RUN)


def live_status():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from wxbridge.app.operations import live_status_path
    return json.loads(live_status_path(APP, RUN).read_text())


def queue_one(path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from wxbridge.storage.inspection import inspect_queue
    return inspect_queue(path).release_summary()


def queue():
    groups = configured_groups()
    from wxbridge.app.operations import queue_paths
    paths = queue_paths(APP, RUN)
    values = {identifier: queue_one(path) for path, identifier in paths.items()}
    if groups[0][0] is None and len(values) == 1:
        return values[None]
    return dict(cursor={k:v['cursor'] for k,v in values.items()}, identity={k:v['identity'] for k,v in values.items()},
                pending=sum(v['pending'] for v in values.values()),
                memory_pending=sum(v['memory_pending'] for v in values.values()))


def listener_idle(snapshot, live, *, allow_pending_memory=False):
    return (not snapshot['pending'] and (allow_pending_memory or not snapshot['memory_pending'])
            and not live.get('blocked_reason') and not live.get('send_blocked')
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


def deploy(stage, backup, *, apply=False, update_helper=False, allow_pending_memory=False,
           allow_removed=(), update_prompts=False, enable_images=False, image_account=None,
           image_account_root=None):
    old = json.loads((APP/'release-manifest.json').read_text())
    new = json.loads((stage/'release-manifest.json').read_text())
    planned = changes(old, new, allow_removed=allow_removed, update_prompts=update_prompts)
    planned = [n for n in planned if update_prompts or n not in PRESERVE or not (APP/n).exists()]
    removed = sorted(set(old['files'])-set(new['files']))
    for name, info in new['files'].items():
        target = stage/name
        if not target.resolve().is_relative_to(stage.resolve()) or sha(target) != info['sha256']:
            raise ValueError('staged_hash_mismatch')
    helper_changed = helper_changes(old, new)
    helper_config = json.loads(HELPER_CONFIG.read_text())
    updated_config = image_configuration(helper_config, enable=enable_images,
                                         account=image_account, account_root=image_account_root)
    images_before = helper_config.get('image_download', {}).get('enabled', False)
    images_after = updated_config.get('image_download', {}).get('enabled', False)
    installation = json.loads((HELPER/'installation.json').read_text())
    if images_after:
        helper_changed += image_helper_changes(new, installation)
        if enable_images:
            # Existing installs may have only text assets even when the app
            # manifest already lists the experimental image source files.
            helper_changed.append('helper.py')
    helper_changed = sorted(set(helper_changed))
    helper_config_changed = updated_config != helper_config
    if HELPER_DROPIN.is_symlink():
        raise ValueError('native_image_stop_policy_unsafe')
    if images_after and HELPER_DROPIN.exists() and HELPER_DROPIN.read_bytes() != IMAGE_STOP_POLICY:
        raise ValueError('native_image_stop_policy_conflict')
    stop_policy_changed = images_after and (not HELPER_DROPIN.is_file() or HELPER_DROPIN.read_bytes() != IMAGE_STOP_POLICY)
    helper_update = bool(helper_changed or helper_config_changed or stop_policy_changed)
    report = dict(commit=new['commit'], base=old['commit'], changed_files=planned,
                  helper_assets=helper_changed, removed_files=removed,
                  update_prompts=update_prompts, native_images=images_after,
                  helper_config_updated=helper_config_changed,
                  helper_stop_policy_updated=stop_policy_changed, applied=False)
    if not apply:
        return report
    import fcntl
    import pwd
    user = pwd.getpwnam('wxbridge')
    if os.geteuid() != 0 or backup.exists() or backup.is_symlink():
        raise ValueError('backup_or_permissions_invalid')
    require_application_layout()
    if not backup.resolve().is_relative_to(Path('/var/backups/wxbridge')):
        raise ValueError('backup_outside_private_root')
    if helper_update and not update_helper:
        raise ValueError('helper_update_requires_explicit_flag')
    for name, info in old['files'].items():
        if name not in PRESERVE and sha(APP/name) != info['sha256']:
            raise ValueError('live_code_changed')
    if helper_update:
        for name in (*ASSETS, *(n for n in IMAGE_ASSETS if n in installation['files'])):
            if name not in installation['files'] or sha(HELPER/name) != installation['files'][name]:
                raise ValueError('live_helper_changed')
        if any(name not in installation['files'] and ((HELPER/name).exists() or (HELPER/name).is_symlink())
               for name in helper_changed):
            raise ValueError('untracked_helper_asset_requires_review')
    mount = run('findmnt', '-n', '-o', 'FSTYPE,OPTIONS', '--target', str(RUN)).stdout
    if mount.split()[0] != 'tmpfs' or 'noswap' not in mount:
        raise ValueError('runtime_mount_invalid')
    before_units = {u: active(u) for u in UNITS}
    if not all(before_units.values()):
        raise ValueError('services_not_ready')
    for _ in range(30):
        q = queue()
        live = live_status()
        if listener_idle(q, live, allow_pending_memory=allow_pending_memory):
            break
        time.sleep(1)
    else:
        raise ValueError('listener_not_idle')
    records = {}
    stopped = False
    mutated = False
    started = time.monotonic()
    try:
        stopped = True
        stop_helper(images_enabled=images_before)
        with ExitStack() as locks:
            lock_paths = {RUN/'queue.sqlite3.lock', RUN/'decrypted/.listener.lock'}
            lock_paths.update(Path(cfg['queue']['file']+'.lock') for _, cfg, _ in configured_groups())
            for path in sorted(lock_paths):
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW);locks.callback(os.close, fd)
                if os.fstat(fd).st_uid != user.pw_uid:
                    raise ValueError('lock_owner_invalid')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = queue()
            if before['pending'] or (before['memory_pending'] and not allow_pending_memory):
                raise ValueError('queue_changed_before_switch')
            preserved_prompts = PRESERVE-set(planned)
            private_files = ['config.json', '.env', 'src/provider/.env', *sorted(preserved_prompts)]
            instance_directory = APP/'instance'
            if instance_directory.is_symlink():
                raise ValueError('unsafe_instance_directory')
            for path in instance_directory.rglob('*'):
                if path.is_symlink():
                    raise ValueError('unsafe_instance_config')
                if path.is_file():
                    private_files.append(str(path.relative_to(APP)))
            private = {n: sha(APP/n) for n in private_files if (APP/n).is_file()}
            memory = {str(p.relative_to(APP)): sha(p) for p in (APP/'memory').rglob('*') if p.is_file()}
            backup.mkdir(parents=True, mode=0o700);backup.chmod(0o700)
            updates = {APP/n: ((stage/n).read_bytes(), 0o755 if new['files'][n]['mode']=='100755' else 0o644, 0, 0) for n in planned}
            updates.update({APP/n: (None, 0, 0, 0) for n in removed})
            updates[APP/'release-manifest.json'] = ((stage/'release-manifest.json').read_bytes(), 0o644, 0, 0)
            if helper_update:
                for name in helper_changed:
                    data = (stage/helper_asset(new, name)).read_bytes()
                    updates[HELPER/name] = (data, 0o644, 0, 0)
                    installation['files'][name] = hashlib.sha256(data).hexdigest()
                installation['review_release'] = new['commit']
                updates[HELPER/'installation.json'] = (json.dumps(installation, indent=2).encode(), 0o600, 0, 0)
                if helper_config_changed:
                    updates[HELPER_CONFIG] = (json.dumps(updated_config, indent=2).encode(), 0o600, 0, 0)
                if stop_policy_changed:
                    updates[HELPER_DROPIN] = (IMAGE_STOP_POLICY, 0o644, 0, 0)
            for path in updates:
                if not (path.resolve().is_relative_to(APP) or path.resolve().is_relative_to(HELPER)
                        or path == HELPER_DROPIN):
                    raise ValueError('install_path_outside_root')
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError('unsafe_install_path')
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
                if data is None:
                    path.unlink()
                else:
                    atomic(path, data, mode, uid, gid)
            if private != {n: sha(APP/n) for n in private} or memory != {str(p.relative_to(APP)): sha(p) for p in (APP/'memory').rglob('*') if p.is_file()} or queue() != before:
                raise ValueError('private_state_changed')
            report.update(cursor_before=before['cursor'], private_state_preserved=True,
                          pending_memory_preserved=before['memory_pending'])
        if helper_update:
            run('systemctl', 'daemon-reload')
            run(str(HELPER/'venv/bin/python'), '-I', '-c',
                "import runpy; runpy.run_path('/opt/wxbridge-native-helper/helper.py')['configuration']('/opt/wxbridge-native-helper/helper-config.json')")
        # Readiness is a metadata-only operation, not a test message or injection.
        stamp = time.time();run('systemctl', 'start', UNITS[0])
        wait_native_ready()
        for _ in range(20):
            live = live_status()
            if active(UNITS[0]) and live.get('updated_at', 0)>=stamp and live.get('build', {}).get('commit')==new['commit'] and not live.get('blocked_reason'):
                break
            time.sleep(1)
        else:
            raise ValueError('new_listener_not_ready')
        if queue()['identity']!=before['identity']:
            raise ValueError('client_or_identity_changed')
        report.update(applied=True, cursor_after=queue()['cursor'], phase=live.get('phase'),
                      maintenance_seconds=round(time.monotonic()-started, 3))
    except BaseException:
        if mutated:
            stop_helper(images_enabled=images_after)
            for name, info in records.items():
                path = Path(name)
                if info['existed']:
                    atomic(path, (backup/name.lstrip('/')).read_bytes(), info['mode'], info['uid'], info['gid'])
                elif path.exists():
                    path.unlink()
            if helper_update:
                run('systemctl', 'daemon-reload')
        raise
    finally:
        if stopped:
            for unit in UNITS:
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
    parser.add_argument('--enable-native-images', action='store_true')
    parser.add_argument('--image-account')
    parser.add_argument('--image-account-root', type=Path)
    parser.add_argument('--update-prompts', action='store_true', help='Explicitly update versioned prompts with backups')
    parser.add_argument('--allow-removed', action='append', default=[], help='Exact reviewed old manifest path to remove; repeat for every removal')
    parser.add_argument('--allow-pending-memory', action='store_true',
                        help='Preserve durable pending memory jobs; no running workers or send tasks allowed')
    args = parser.parse_args()
    try:
        print(json.dumps(deploy(args.stage, args.backup, apply=args.apply, update_helper=args.update_native_helper,
                               allow_pending_memory=args.allow_pending_memory, allow_removed=args.allow_removed,
                               update_prompts=args.update_prompts, enable_images=args.enable_native_images,
                               image_account=args.image_account,
                               image_account_root=args.image_account_root), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps(dict(applied=False, error=type(exc).__name__, reason=str(exc) if isinstance(exc, ValueError) else 'deployment_failed')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
