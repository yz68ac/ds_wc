"""Optional trusted image worker; loaded only from audited helper-owned assets.

No application imports, decrypted database access, desktop operations or output.
The worker stays alive while native callbacks are owned, even after caller timeout.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import threading
import time

if __package__:
    from . import image_contract as contract
else:
    _spec = importlib.util.spec_from_file_location('_wechat_image_contract', Path(__file__).with_name('image_contract.py'))
    contract = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(contract)


def account_matches(pid, root):
    """Bind to the client's CURRENT open account files, never inspect DB contents."""
    roots = set()
    try:
        for fd in (Path('/proc')/str(pid)/'fd').iterdir():
            try:
                target = os.readlink(fd)
                if '/db_storage/' in target and not target.endswith(' (deleted)'):
                    roots.add(target.split('/db_storage/', 1)[0])
            except OSError:
                continue
    except OSError:
        return False
    return roots == {root}


def process_identity(pid):
    try:
        return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def summarize(status, armed):
    requested = None if armed else False
    if status.get('download_call_returned') is True:
        requested = True
    if contract.safe_to_unload(status) and status.get('downloads') == 0:
        requested = False
    if contract.completed(status):
        return dict(ok=True, state='native_completed', requested=True, source_verified=True, download_status=[0, 0])
    if not status.get('cleanup'):
        return contract.failure('native_image_cleanup_failed', requested)
    error = status.get('error')
    code = {'message_binding_mismatch': 'native_image_source_mismatch',
            'account_changed': 'native_image_account_mismatch', 'message_not_found': 'native_image_not_found',
            'query_timeout': 'native_image_timeout'}.get(error, 'native_image_failed')
    if status.get('unconfirmed') or status.get('cancelled'):
        code = 'native_image_timeout'
    return contract.failure(code, requested)


def drive(script, *, deadline, stop, alive, clock=time.monotonic, sleep=time.sleep):
    """Only return when unloading is safe, or the original client no longer exists.

    RPC loss is not proof that a coroutine ended. Keep the worker quarantined and
    retry READ-ONLY status while the parent can respond 'pending' to its client.
    """
    cancel_requested = False
    while alive():
        if (stop.is_set() or clock() >= deadline) and not cancel_requested:
            try:
                script.exports_sync.cancel()
                cancel_requested = True
            except Exception:
                pass
        try:
            status = script.exports_sync.status()
            if contract.safe_to_unload(status):
                return status, True
        except Exception:
            pass
        sleep(.05)
    return {}, False


def run(config, value, stop, find_client):
    import fcntl
    if stop.is_set():
        return contract.failure('native_image_timeout')
    assets = Path(config['assets'])
    options = config['image_download']
    contract.validate(value, config['allowed_chat_ids'], options['expected_account'])
    # Shared across helper instances for this Linux UID; never unlink a live lock.
    fd = os.open('/run/wechat-linux-image-'+str(config['uid'])+'.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            return contract.failure('native_image_failed')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return contract.failure('native_image_busy')
        profile = json.loads((assets/'image_profile.json').read_text(encoding='utf-8'))
        if profile.get('backend') != 'image_coroutine_v1':
            return contract.failure('native_image_profile_mismatch')
        try:
            pid = find_client(config['uid'], profile['sha256'])
        except ValueError as exc:
            return contract.failure('native_image_profile_mismatch' if str(exc) == 'native_profile_mismatch' else 'native_image_wrong_client')
        if not account_matches(pid, options['account_root']):
            return contract.failure('native_image_account_mismatch')
        identity = process_identity(pid)
        if identity is None:
            return contract.failure('native_image_wrong_client')
        try:
            import frida
        except ImportError:
            return contract.failure('native_image_dependency_missing')
        session = script = None
        armed = loaded = False
        safe = False
        status = {}
        deadline = time.monotonic()+min(value['timeout_seconds'], options['timeout_seconds'])
        alive = lambda: process_identity(pid) == identity
        try:
            session = frida.get_local_device().attach(pid)
            binding = {k: value[k] for k in ('server_id', 'sender', 'image_md5')}
            binding.update(pid=pid, mode='coroutine-download', chat=value['chat_id'],
                           timeout_ms=min(value['timeout_seconds'], options['timeout_seconds'])*1000)
            source = ('const PROFILE='+json.dumps(profile)+';\nconst CONFIG='+json.dumps(binding)+';\n'
                      'const C_SOURCE='+json.dumps((assets/'image_adapter.c').read_text(encoding='utf-8'))+';\n'
                      +(assets/'image_agent.js').read_text(encoding='utf-8'))
            script = session.create_script(source)
            # Never forward script exceptions/data (which can contain identifiers).
            script.on('message', lambda message, data: None)
            script.load()
            loaded = True
            # A load-time JS exception may only emit a message. Prove exports
            # exist before arming; otherwise a known pre-call failure could be
            # mistaken for an ambiguous armed callback and wait forever.
            initial = script.exports_sync.status()
            if initial.get('stage') != 'ready' or not contract.safe_to_unload(initial):
                return contract.failure('native_image_failed')
            if stop.is_set():
                return contract.failure('native_image_timeout')
            if not account_matches(pid, options['account_root']):
                return contract.failure('native_image_account_mismatch')
            armed = True  # An arm RPC failure may occur AFTER enqueuing its source.
            response = script.exports_sync.arm()
            if response.get('accepted') is not True:
                armed = False
                return contract.failure('native_image_failed')
            status, safe = drive(script, deadline=deadline, stop=stop, alive=alive)
            if not alive():
                return contract.failure('native_image_wrong_client', None)
            if not account_matches(pid, options['account_root']):
                return contract.failure('native_image_account_mismatch', None)
            return summarize(status, armed)
        except Exception:
            return contract.failure('native_image_failed', None if armed else False)
        finally:
            if script and loaded and armed and not safe and alive():
                # This can outlive the IPC request. Never kill a CModule worker.
                try:
                    script.exports_sync.cancel()
                except Exception:
                    pass
                status, safe = drive(script, deadline=deadline, stop=stop, alive=alive)
            if script and (safe or not armed):
                try:
                    script.unload()
                except Exception:
                    pass
            if session:
                try:
                    session.detach()
                except Exception:
                    pass
    finally:
        os.close(fd)


class ImageManager:
    """One optional helper thread; request history refuses eviction/re-submission."""

    def __init__(self, config, find_client):
        self.config, self.find_client = config, find_client
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-image')
        self.stop = threading.Event()
        self.history = {}
        self.active = None

    @property
    def busy(self):
        return self.active is not None and not self.active.done()

    def handle(self, value):
        if (isinstance(value, dict) and set(value) == {'op', 'request_id'}
                and value.get('op') == 'image_status' and contract.valid_id(value['request_id'])):
            old = self.history.get(value['request_id'])
            if old is None:
                return contract.failure('native_image_request_missing', None)
            return self._result(old[1])
        try:
            contract.validate(value, self.config['allowed_chat_ids'], self.config['image_download']['expected_account'])
        except ValueError as exc:
            return contract.failure(str(exc))
        identity = {k: v for k, v in value.items() if k != 'timeout_seconds'}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        old = self.history.get(value['request_id'])
        if old:
            return self._result(old[1]) if old[0] == digest else contract.failure('native_image_request_conflict')
        if self.busy:
            return contract.failure('native_image_busy')
        if len(self.history) >= 4096:
            return contract.failure('native_image_history_full')
        future = self.executor.submit(run, self.config, dict(value), self.stop, self.find_client)
        self.active = future
        self.history[value['request_id']] = (digest, future)
        try:
            return future.result(timeout=1)
        except FutureTimeout:
            return dict(ok=True, state='pending', requested=None)
        except Exception:
            return contract.failure('native_image_failed', None)

    @staticmethod
    def _result(future):
        if not future.done():
            return dict(ok=True, state='pending', requested=None)
        try:
            return future.result()
        except Exception:
            return contract.failure('native_image_failed', None)

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
