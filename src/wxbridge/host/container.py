"""Validated container initialization and supervisor liveness probe."""
import json
import os
from pathlib import Path
import shutil
import sys

from .layout import code_root, config_root, sensitive_root
from .system import atomic_json


def private_directory(path, uid, gid):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('container_directory_symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid not in (0, uid):
        raise ValueError('container_directory_owner')
    os.chown(path, uid, gid)
    path.chmod(0o700)


def prepare_web(path):
    from .settings import read_web, web_document
    host_network=os.environ.get('WXBRIDGE_NETWORK')=='host'
    if not path.exists():
        atomic_json(path,dict(host='127.0.0.1' if host_network else '0.0.0.0',port=8730))
    settings=web_document(read_web(path))
    previous=dict(settings)
    if host_network:
        if settings['host']=='0.0.0.0':settings['host']='127.0.0.1'
        if 'WXBRIDGE_WEB_PORT' in os.environ:
            port=int(os.environ['WXBRIDGE_WEB_PORT'])
            if not 1<=port<=65535:raise ValueError('web_port_invalid')
            settings['port']=port
    if settings!=previous:atomic_json(path,settings)


def prepare():
    os.umask(0o077)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import fcntl
    import pwd
    from .processes import validate_namespace
    from wxbridge.storage.memoryfs import is_noswap_tmpfs
    validate_namespace()
    account = pwd.getpwnam('wxbridge')
    root, sensitive = config_root(), sensitive_root()
    private_directory(root, account.pw_uid, account.pw_gid)
    # Keep this descriptor across exec, so duplicate containers cannot clear RAM.
    lock = os.open(root/'.account.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock, True)
    mounts = Path('/proc/self/mountinfo').read_text()
    runtime = Path('/run/wxbridge-trial')
    for path, noswap in ((sensitive, True), (runtime, False)):
        if not is_noswap_tmpfs(path.resolve(), mounts, require_noswap=noswap):
            raise ValueError('container_mount_policy')
        if path.stat().st_uid != account.pw_uid or path.stat().st_mode & 0o077:
            raise ValueError('container_mount_permissions')
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    for path, uid, gid in ((Path(account.pw_dir), account.pw_uid, account.pw_gid),
                            (root/'instance', account.pw_uid, account.pw_gid),
                            (Path('/etc/wxbridge'), 0, account.pw_gid),
                            (Path('/var/lib/wxbridge-service'), 0, 0)):
        private_directory(path, uid, gid)
    Path('/etc/wxbridge').chmod(0o750)
    private_directory(Path('/run/wxbridge-control'), 0, account.pw_gid)
    Path('/run/wxbridge-control').chmod(0o750)
    private_directory(Path('/run/wechat-linux-images'), 0, 0)
    profile = json.loads((code_root()/'src/wechat_linux/native/profile.json').read_text())
    import hashlib
    with open('/opt/wechat/wechat', 'rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != profile['sha256']:
            raise ValueError('native_profile_mismatch')
    config = root/'config.json'
    if not config.exists():
        document = dict(db_dir='', state_file='/run/wxbridge-trial/state.json',
                        log_file='/run/wxbridge-trial/bot.log',
                        queue=dict(file='/run/wxbridge-trial/queue.sqlite3'),
                        memory=dict(directory='/var/lib/wxbridge/memory'),
                        ui=dict(backend='native_service',dry_run=False),
                        helper_transport=dict(enabled=True,socket='/run/wxbridge-trial/native-helper.sock',
                                              allowed_chat_ids=['filehelper'],max_text_bytes=4096),
                        submission_api=dict(enabled=False,socket='/run/wxbridge-trial/native-api.sock',
                                            max_pending=16,max_requests=1024))
        document.update(provider_env='/run/secrets/wxbridge/provider.env',
                        keys_file=str(sensitive/'keys.json'), decrypted_dir=str(sensitive/'decrypted'))
        atomic_json(config, document, uid=account.pw_uid, gid=account.pw_gid)
    from .credentials import validate_provider
    document = json.loads(config.read_text(encoding='utf-8'))
    # Upgrade the earlier RAM-copy layout without changing user-selected files.
    if document.get('provider_env') == str(sensitive/'provider.env'):
        document['provider_env'] = '/run/secrets/wxbridge/provider.env'
        atomic_json(config, document, uid=account.pw_uid, gid=account.pw_gid)
    validate_provider(Path(document['provider_env']))
    from wxbridge.config import load_account
    cfg = load_account(config)
    if Path(cfg.keys_file) != sensitive/'keys.json' or Path(cfg.decrypted_dir) != sensitive/'decrypted':
        raise ValueError('container_sensitive_paths_mismatch')
    persistent = Path('/etc/wxbridge/boot.json')
    if not persistent.exists():
        atomic_json(persistent, dict(db_dir=''))
    helper = Path('/etc/wxbridge/helper-config.json')
    if not helper.exists():
        atomic_json(helper,dict(socket='/run/wxbridge-trial/native-helper.sock',uid=account.pw_uid,
                              allowed_chat_ids=['filehelper'], assets='/opt/wxbridge-native-helper',
                              worker_timeout_seconds=15))
    web = Path('/etc/wxbridge/web.json')
    prepare_web(web)
    password = Path('/etc/wxbridge/web-password')
    if not password.exists():
        raise ValueError('provide_private_settings_web_password_file')
    if password.is_symlink() or password.stat().st_mode & 0o077:
        raise ValueError('unsafe_web_password')
    os.chown(password, account.pw_uid, account.pw_gid)
    return lock


def main():
    if len(sys.argv) in (3,4) and sys.argv[1]=='initialize':
        from .bootstrap import initialize_data
        initialize_data(Path(sys.argv[2]),Path(sys.argv[3]) if len(sys.argv)==4 else None)
        return 0
    if sys.argv[1:] == ['health']:
        from wxbridge.control import request
        print(json.dumps(request('status')))
        return 0
    prepare()
    os.execv(str(code_root()/'.venv/bin/python'),
             [str(code_root()/'.venv/bin/python'),'-m','wxbridge'])


if __name__ == '__main__':
    raise SystemExit(main())
