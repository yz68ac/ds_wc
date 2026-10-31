"""Upgrade an existing host from reviewed source and a prepared, fixed-path venv.

prepare installs into a new environment without stopping the service. apply
backs up changed files, pauses the bot, switches the environment and restarts
the unit with the bot paused. resume restores the previous bot enable state
after read-only acceptance. rollback restores code/configuration, never queues.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

APP = Path('/opt/wxbridge')
UNIT = Path('/etc/systemd/system/wxbridge.service')
LAUNCHER = Path('/usr/local/bin/wxbridge')
STATE = Path('/var/lib/wxbridge-service/state.json')
BACKUPS = Path('/var/backups/wxbridge')
ENVIRONMENTS = Path('/opt/wxbridge-envs')
SCANNER = Path('/usr/local/lib/wxbridge-recovery')
PRESERVE = {'prompts/system.md', 'prompts/memory.md', 'prompts/topic_aliases.json'}
RECOVERY_MODULES = (
    'wxbridge/__init__.py', 'wxbridge/storage/__init__.py', 'wxbridge/storage/memoryfs.py',
    'wechat_linux/__init__.py', 'wechat_linux/models.py', 'wechat_linux/errors.py',
    'wechat_linux/storage/__init__.py', 'wechat_linux/storage/crypto.py', 'wechat_linux/storage/keyscan.py',
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def plain_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_in_upgrade_path')
    return path


def atomic(path, data, mode=0o600, uid=None, gid=None):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.upgrade-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode) if hasattr(os, 'fchmod') else os.chmod(name, mode)
            if uid is not None and hasattr(os, 'fchown'):
                os.fchown(stream.fileno(), uid, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    atomic(path, (json.dumps(value, indent=2, sort_keys=True)+'\n').encode())


def run(*args, timeout=60):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True,
                          text=True, timeout=timeout, cwd=APP)


def control(action):
    # Reuse the installed Unix control protocol; no model or WeChat operations.
    return runpy.run_path(str(APP/'src/wxbridge_web/control.py'))['request'](action)


def rewrite_unit(text):
    lines = text.splitlines()
    indices = [i for i, line in enumerate(lines) if line.startswith('ExecStart=')]
    if len(indices) != 1:
        raise ValueError('ambiguous_service_entry')
    line = lines[indices[0]]
    prefix = 'ExecStart=/opt/wxbridge/.venv/bin/python '
    old, new = '/opt/wxbridge/scripts/service.py run', '-m wxbridge_service --root /opt/wxbridge'
    if not line.startswith(prefix) or not (line[len(prefix):] == old or line[len(prefix):].startswith(old+' ')
                                         or line[len(prefix):] == new or line[len(prefix):].startswith(new+' ')):
        raise ValueError('unknown_service_entry')
    lines[indices[0]] = prefix+line[len(prefix):].replace(old, new, 1)
    return ('\n'.join(lines)+'\n').encode()


def context_upgrade(data, max_chars=None):
    from wxbridge.config import migrate_context_data
    result = migrate_context_data(deepcopy(data))
    target = result.setdefault('target', {})
    for key, value in dict(context_recent_messages=30, context_max_age_seconds=1800,
                           context_bot_messages=3, context_budget_strategy='recent').items():
        target.setdefault(key, value)
    if max_chars is not None:
        if type(max_chars) is not int or not 1 <= max_chars <= 100000:
            raise ValueError('invalid_context_limit')
        target['context_max_chars'] = max_chars
    return result


def source_updates(stage, baseline):
    """The reviewed actual-file baseline also covers stale incremental manifests."""
    rules = runpy.run_path(str(stage/'scripts/check-repository.py'))
    manifest = json.loads((stage/'release-manifest.json').read_text())
    if not re.fullmatch('[a-f0-9]{40}', manifest.get('commit', '')):
        raise ValueError('invalid_release_commit')
    expected = baseline['files']
    updates = {}
    for name, item in manifest['files'].items():
        parts = Path(name).parts
        if (not name or Path(name).is_absolute() or '..' in parts or '\\' in name or ':' in name
                or rules['private_path'](name) or item.get('mode') not in ('100644', '100755')):
            raise ValueError('invalid_upgrade_source')
        if name in PRESERVE and (APP/name).exists():
            continue
        source = plain_path(stage/name)
        if not source.is_relative_to(stage) or digest(source) != item['sha256']:
            raise ValueError('staged_hash_mismatch')
        path = plain_path(APP/name)
        if digest(path) != expected.get(name):
            raise ValueError('reviewed_source_changed:'+name)
        if digest(path) != item['sha256']:
            updates[path] = (source.read_bytes(), 0o755 if item['mode']=='100755' else 0o644)
    for name, old_hash in expected.items():
        if name in manifest['files'] or name in PRESERVE or old_hash is None:
            continue
        # A baseline is not permission to delete runtime/private data.
        if (Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name or ':' in name
                or rules['private_path'](name)):
            raise ValueError('private_path_in_upgrade_baseline')
        path = plain_path(APP/name)
        if digest(path) != old_hash:
            raise ValueError('reviewed_source_changed:'+name)
        updates[path] = (None, 0)
    updates[APP/'release-manifest.json'] = ((stage/'release-manifest.json').read_bytes(), 0o644)
    return manifest, updates


def verify_wheels(stage, wheels):
    expected = {'wxbridge', 'model_provider', 'wechat_linux_adapter'}
    found, result = set(), {}
    mappings = {'wxbridge/':'src/wxbridge/', 'wxbridge_service/':'src/wxbridge_service/',
                'wxbridge_web/':'src/wxbridge_web/', 'provider/':'src/provider/',
                'wechat_linux/':'src/wechat_linux/'}
    for wheel in sorted(wheels.glob('*.whl')):
        name = wheel.name.split('-', 1)[0]
        if name not in expected or name in found:
            raise ValueError('unexpected_upgrade_wheel')
        found.add(name)
        with zipfile.ZipFile(wheel) as archive:
            count = 0
            for entry in archive.namelist():
                prefix = next((p for p in mappings if entry.startswith(p)), None)
                if prefix is None:
                    continue
                source = stage/(mappings[prefix]+entry[len(prefix):])
                if not source.is_file() or archive.read(entry).replace(b'\r\n', b'\n') != source.read_bytes().replace(b'\r\n', b'\n'):
                    raise ValueError('wheel_source_mismatch')
                count += 1
            if not count:
                raise ValueError('empty_upgrade_wheel')
        result[str(wheel)] = digest(wheel)
    if found != expected:
        raise ValueError('missing_upgrade_wheel')
    return result


def recovery_updates(stage):
    manifest = json.loads((stage/'release-manifest.json').read_text())
    updates = {}
    for name in RECOVERY_MODULES:
        source = plain_path(stage/'src'/name)
        if digest(source) != manifest['files']['src/'+name]['sha256']:
            raise ValueError('staged_recovery_hash_mismatch')
        updates[plain_path(SCANNER/name)] = (source.read_bytes(), 0o644)
    return updates


def check_recovery_projection(stage):
    # The privileged scanner uses system Python, independently of the app venv.
    with tempfile.TemporaryDirectory(prefix='wxbridge-recovery-', dir=stage.parent) as directory:
        root = Path(directory)
        for destination, (data, mode) in recovery_updates(stage).items():
            atomic(root/destination.relative_to(SCANNER), data, mode)
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from wechat_linux.storage.keyscan import extract_keys;'
                'from wechat_linux.storage.crypto import AES;'
                'from wxbridge.storage.memoryfs import is_noswap_tmpfs;'
                'assert AES is not None;'
                'assert not any(n in sys.modules for n in ("provider","gi","frida"))')
        run(sys.executable, '-I', '-c', code, directory)


class FileTransaction:
    def __init__(self, backup):
        self.backup, self.records = plain_path(backup), {}

    def snapshot(self, updates):
        self.backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        for index, path in enumerate(updates):
            path = plain_path(path)
            info = path.stat() if path.exists() else None
            record = dict(existed=info is not None, saved=str(index))
            if info:
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('nonregular_upgrade_target')
                atomic(self.backup/str(index), path.read_bytes())
                record.update(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
            self.records[str(path)] = record
        save_json(self.backup/'files.json', self.records)

    def write(self, updates):
        for path, (data, mode) in updates.items():
            if data is None:
                path.unlink()
            else:
                old = self.records[str(path)]
                atomic(path, data, mode, old.get('uid'), old.get('gid'))

    def restore(self):
        records = self.records or json.loads((self.backup/'files.json').read_text())
        for name, item in records.items():
            path = plain_path(name)
            if item['existed']:
                atomic(path, (self.backup/item['saved']).read_bytes(), item['mode'], item['uid'], item['gid'])
            elif path.exists():
                path.unlink()


def private_hashes():
    files = [APP/'.env', APP/'src/provider/.env', *(APP/'instance').rglob('*'),
             *(APP/'memory').rglob('*')]
    return {str(p): digest(plain_path(p)) for p in files if p.is_file()}


def prepare(stage, wheels, backup, baseline, max_chars):
    stage, wheels, backup = plain_path(stage), plain_path(wheels), plain_path(backup)
    if not backup.is_relative_to(BACKUPS) or backup == BACKUPS or backup.exists():
        raise ValueError('invalid_upgrade_backup')
    manifest, updates = source_updates(stage, baseline)
    updates.update(recovery_updates(stage))
    check_recovery_projection(stage)
    wheel_hashes = verify_wheels(stage, wheels)
    environment = ENVIRONMENTS/(manifest['commit']+'-'+str(time.time_ns()))
    plain_path(environment)
    ENVIRONMENTS.mkdir(mode=0o755, exist_ok=True)
    run(sys.executable, '-m', 'venv', '--system-site-packages', environment, timeout=120)
    python = environment/'bin/python'
    # Use the exported, pinned closure; no secrets or provider configuration are installed.
    run(python, '-m', 'pip', '--disable-pip-version-check', 'install', '--no-deps',
        '-r', stage/'requirements-test.txt', *wheel_hashes, timeout=1200)
    # GI comes from Debian's system site-packages. Validate the application's
    # complete dependency closure without treating unrelated apt metadata as pip packages.
    run(python, '-I', '-c', 'from wxbridge_service.install import dependency_health;'
        'failures=dependency_health();raise SystemExit(bool(failures))')
    run(python, '-I', '-c', 'from wxbridge.app.bot import GroupSession;'
        'from wechat_linux.desktop.login import LoginController;from provider import create_client')
    sys.path.insert(0, str(stage/'src'))
    configurations = {}
    for path in sorted((APP/'instance').glob('*/config.json')):
        data = json.loads(plain_path(path).read_text(encoding='utf-8-sig'))
        new = context_upgrade(data, max_chars)
        configurations[str(path)] = dict(before=digest(path), data=new)
    backup.mkdir(parents=True, mode=0o700)
    record = dict(schema=1, stage=str(stage), environment=str(environment),
                  manifest=digest(stage/'release-manifest.json'), wheels=wheel_hashes,
                  scanner_hashes={str(p):digest(p) for p in recovery_updates(stage)},
                  baseline=baseline, configurations=configurations, phase='prepared')
    save_json(backup/'upgrade.json', record)
    return dict(phase='prepared', commit=manifest['commit'], backup=str(backup),
                changed_files=len(updates), configurations=len(configurations))


def wait_stopped(deadline=360):
    until = time.monotonic()+deadline
    while time.monotonic() < until:
        if control('status')['components']['bot']['state'] == 'inactive':
            return
        time.sleep(.5)
    raise ValueError('bot_did_not_drain')


def load_record(backup):
    backup = plain_path(backup)
    if not backup.is_relative_to(BACKUPS) or backup == BACKUPS:
        raise ValueError('invalid_upgrade_backup')
    return backup, json.loads(plain_path(backup/'upgrade.json').read_text())


def apply(backup):
    backup, record = load_record(backup)
    if record['phase'] != 'prepared':
        raise ValueError('upgrade_not_prepared')
    stage, environment = Path(record['stage']), Path(record['environment'])
    if digest(stage/'release-manifest.json') != record['manifest']:
        raise ValueError('prepared_release_changed')
    if any(digest(path) != value for path, value in record['wheels'].items()):
        raise ValueError('prepared_wheels_changed')
    manifest, updates = source_updates(stage, record['baseline'])
    if 'scanner_hashes' not in record:
        raise ValueError('upgrade_requires_reprepare')
    if any(digest(path) != value for path, value in record['scanner_hashes'].items()):
        raise ValueError('recovery_changed_after_prepare')
    updates.update(recovery_updates(stage))
    for name, config in record['configurations'].items():
        path = Path(name)
        if digest(path) != config['before']:
            raise ValueError('instance_changed_after_prepare')
        updates[path] = ((json.dumps(config['data'], ensure_ascii=False, indent=2)+'\n').encode(), 0o600)
    updates[UNIT] = (rewrite_unit(UNIT.read_text()), 0o644)
    updates[LAUNCHER] = (b'#!/bin/sh\nexec /opt/wxbridge/.venv/bin/python -m wxbridge "$@"\n', 0o755)
    original = json.loads(STATE.read_text())
    record.update(original_state=original, phase='draining')
    save_json(backup/'upgrade.json', record)
    switched = stopped = False
    transaction = FileTransaction(backup/'files')
    try:
        control('stop')
        wait_stopped()
        # The supervisor drains native callbacks and checkpoints queues before exiting.
        run('systemctl', 'stop', 'wxbridge.service', timeout=None)
        stopped = True
        source_updates(stage, record['baseline'])
        for name, config in record['configurations'].items():
            if digest(name) != config['before']:
                raise ValueError('instance_changed_while_draining')
        record['private_hashes'] = private_hashes()
        transaction.snapshot(updates)
        record.update(phase='switching', old_environment=str(APP/('.venv.before-'+backup.name)))
        if (APP/'.venv').is_symlink():
            record['old_environment_link'] = os.readlink(APP/'.venv')
        elif Path(record['old_environment']).exists():
            raise ValueError('old_environment_backup_exists')
        save_json(backup/'upgrade.json', record)
        transaction.write(updates)
        current = APP/'.venv'
        if current.is_symlink():
            current.unlink()
        else:
            current.rename(record['old_environment'])
        switched = True
        current.symlink_to(environment, target_is_directory=True)
        run(current/'bin/python', '-I', '-c',
            "from wxbridge.config import load_config;load_config('/opt/wxbridge/config.json')")
        for path, value in record['private_hashes'].items():
            if path not in record['configurations'] and digest(path) != value:
                raise ValueError('private_data_changed')
        record.update(phase='awaiting_acceptance', commit=manifest['commit'],
                      installed_hashes={str(path):digest(path) for path in updates})
        save_json(backup/'upgrade.json', record)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', 'wxbridge.service')
        return dict(phase=record['phase'], commit=manifest['commit'], bot_paused=True, backup=str(backup))
    except BaseException:
        if stopped:
            if switched:
                (APP/'.venv').unlink(missing_ok=True)
                if 'old_environment_link' in record:
                    (APP/'.venv').symlink_to(record['old_environment_link'], target_is_directory=True)
                else:
                    Path(record['old_environment']).rename(APP/'.venv')
            if transaction.records:
                transaction.restore()
            save_json(STATE, original)
            run('systemctl', 'daemon-reload')
            run('systemctl', 'start', 'wxbridge.service')
        else:
            save_json(STATE, original)
            if original['bot_enabled']:
                control('start')
        record['phase'] = 'rolled_back'
        save_json(backup/'upgrade.json', record)
        raise


def finish(backup, *, rollback=False):
    backup, record = load_record(backup)
    if record['phase'] not in (('awaiting_acceptance', 'complete') if rollback else ('awaiting_acceptance',)):
        raise ValueError('upgrade_not_awaiting_acceptance')
    if rollback:
        if any(digest(path) != value for path, value in record['installed_hashes'].items()):
            raise ValueError('rollback_target_changed')
        if not (APP/'.venv').is_symlink() or (APP/'.venv').resolve() != Path(record['environment']):
            raise ValueError('active_environment_changed')
        control('stop')
        wait_stopped()
        run('systemctl', 'stop', 'wxbridge.service', timeout=None)
        (APP/'.venv').unlink()
        if 'old_environment_link' in record:
            (APP/'.venv').symlink_to(record['old_environment_link'], target_is_directory=True)
        else:
            Path(record['old_environment']).rename(APP/'.venv')
        FileTransaction(backup/'files').restore()
        save_json(STATE, record['original_state'])
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', 'wxbridge.service')
        record['phase'] = 'rolled_back'
    else:
        for path, value in record['private_hashes'].items():
            if path not in record['configurations'] and digest(path) != value:
                raise ValueError('private_data_changed')
        state = control('status')
        if state['components']['wechat']['state'] != 'active' or state['components']['sender']['state'] != 'active':
            raise ValueError('desktop_or_sender_not_ready')
        if record['original_state']['bot_enabled']:
            run(APP/'.venv/bin/python', APP/'scripts/boot-recovery.py', 'prepare', timeout=240)
            control('start')
        record['phase'] = 'complete'
    save_json(backup/'upgrade.json', record)
    return dict(phase=record['phase'], commit=record.get('commit'), backup=str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'resume', 'rollback'))
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--wheels', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--context-max-chars', type=int)
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        parser.error('root is required')
    try:
        if args.action == 'prepare':
            if not all((args.stage, args.wheels, args.baseline)):
                parser.error('prepare requires --stage, --wheels and --baseline')
            result = prepare(args.stage.resolve(), args.wheels.resolve(), args.backup,
                             json.loads(args.baseline.read_text()), args.context_max_chars)
        elif args.action == 'apply':
            result = apply(args.backup)
        else:
            result = finish(args.backup, rollback=args.action=='rollback')
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # Never print subprocess output, private configuration or arbitrary exceptions.
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z_]{1,80}', str(exc)) else type(exc).__name__
        print(json.dumps(dict(phase='failed', reason=reason)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
