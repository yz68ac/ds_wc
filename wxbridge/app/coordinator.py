"""Multiple logical group sessions; all WeChat reads stay on this main loop."""
from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
import time

from wechat_linux.native.protocol import settings as native_settings
from wechat_linux.storage.store import StoreError
from wxbridge.app.account import AccountRuntime
from wxbridge.app.bot import GroupSession
from wxbridge.instances import enabled_instances
from wxbridge.app.build_info import runtime_identity

log = logging.getLogger('wxbridge')


def read_account_status(cfg):
    from wxbridge.app.monitor import read_bytes
    path = Path(cfg['state_file']).parent/'account-status.json'
    if not path.exists():
        return dict(phase='not_started', instances=[e['id'] for e in cfg['instances'] if e['enabled']])
    return json.loads(read_bytes(path, 256*1024))


class Coordinator:
    def __init__(self, cfg, echo=False, *, store=None):
        self.cfg, self.echo = cfg, echo
        self.account = AccountRuntime(cfg, store=store)
        self.sessions = self.account.sessions
        self.native_api = None
        self.closed = False
        self.rotation, self.generation_rotation, self.delivery_rotation = 0, 0, 0
        self.started_at, self.build = time.time(), runtime_identity(cfg)
        self.status_path = Path(cfg['state_file']).parent/'account-status.json'
        self.status_signature, self.status_written_at = None, 0
        entries = [e for e in cfg['instances'] if e['enabled']]
        configs = enabled_instances(cfg)
        if not configs:
            raise ValueError('no_enabled_instances')
        # Verify every mapping before recovering any queues. A display name is
        # never accepted as another group's stable identity.
        try:
            for entry in entries:
                resolved, _ = self.account.store.resolve_chat(entry['chat'])
                if resolved != entry['chat'] or not self.account.store.self_wxid:
                    raise StoreError('instance_chat_identity_mismatch')
                if entry['reuse_legacy_state']:
                    legacy, _ = self.account.store.resolve_chat(cfg['target']['chat'])
                    if legacy != resolved:
                        raise StoreError('legacy_state_chat_mismatch')
            for identifier, active in configs:
                session = GroupSession(active, echo=echo, store=self.account.store, account=self.account,
                                       instance_id=identifier, start_gateway=False)
                self.sessions.append(session)
            options = native_settings(cfg)
            if options['enabled'] and not cfg['ui'].get('dry_run'):
                from wechat_linux.transport.server import NativeServer
                from wxbridge.app.native_gateway import AccountNativeService
                self.native_api = NativeServer(options, AccountNativeService(self, options))
        except BaseException:
            self.close()
            raise

    def tick(self):
        order = self.sessions[self.rotation:]+self.sessions[:self.rotation]
        self.rotation = (self.rotation+1) % len(self.sessions)
        # Intake all groups before giving foreground work any API slots.
        for session in order:
            session._poll_messages()
        if self.native_api:
            self.native_api.tick()
        for session in order:
            if session.proactive:
                session.proactive.priority(time.time())
            with session.metrics.measure('generation'):
                session._poll_generation_results()
            with session.metrics.measure('ui_result'):
                session._ui_result_step()
            with session.metrics.measure('confirmation'):
                session._confirm_step()
        # At most one new foreground request per group per pass. A quiet group
        # can use remaining slots, while a busy group cannot consume them first.
        for _ in range(self.account.capacity):
            candidates = self.sessions[self.generation_rotation:]+self.sessions[:self.generation_rotation]
            for session in candidates:
                with session.metrics.measure('generation'):
                    started = session._start_generations(max_new=1)
                if started:
                    self.generation_rotation = (self.sessions.index(session)+1) % len(self.sessions)
                    break
            else:
                break
        for session in order:
            if session.proactive:
                with session.metrics.measure('participation'):
                    session.proactive.step(time.time())
        send_order = self.sessions[self.delivery_rotation:]+self.sessions[:self.delivery_rotation]
        for session in send_order:
            free = self.account.sending() is None
            with session.metrics.measure('delivery'):
                session._send_step()
            if free and self.account.sending() is not None:
                self.delivery_rotation = (self.sessions.index(session)+1) % len(self.sessions)
                break
        for session in order:
            with session.metrics.measure('memory'):
                session._memory_step()
            session.maintain()
        self.save_status()

    def save_status(self, *, stopped=False):
        status = dict(schema=1, pid=os.getpid(), updated_at=time.time(), started_at=self.started_at,
                      phase='stopped' if stopped else 'listening', build=self.build,
                      **self.account.status(), instances={})
        status['generation_workers'] = sum(len(s.generators) for s in self.sessions)
        status['memory'] = dict(running=any(s.memory_job for s in self.sessions))
        status['proactive'] = dict(outcome='evaluating' if any(s.proactive and s.proactive.job for s in self.sessions) else 'idle')
        status['inactive_queues'] = {s.instance_id: dict(queue_length=len(s.journal.tasks()),
            unconfirmed=any(t.get('intent') for t in s.journal.tasks())) for s in self.account.inactive}
        status['blocked_reason'] = 'account_send_unconfirmed' if status['send_blocked'] else ''
        for session in self.sessions:
            value = session.journal.status()
            status['instances'][session.instance_id] = dict(
                queue_length=value['queue_length'], cursor=value['cursor'],
                blocked_reason=value.get('blocked_reason', ''), last_success=value['last_success'],
                model_workers=len(session.generators)+int(session.memory_job is not None)+
                              int(bool(session.proactive and session.proactive.job)))
        signature = json.dumps({k:v for k,v in status.items() if k != 'updated_at'}, sort_keys=True)
        clock = time.monotonic()
        if not stopped and signature == self.status_signature and clock-self.status_written_at < 2:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.status_path.with_suffix('.tmp')
        with open(temporary, 'w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(status, stream, ensure_ascii=False)
        os.replace(temporary, self.status_path)
        self.status_signature, self.status_written_at = signature, clock

    def run(self, once=False):
        try:
            while True:
                self.tick()
                if once:
                    return
                time.sleep(min(float(self.cfg['poll_interval_seconds']), 1.0))
        except KeyboardInterrupt:
            log.info('停止监听，未完成任务保留在内存队列')

    def close(self):
        if self.closed:
            return
        self.closed = True
        with ExitStack() as stack:
            stack.callback(self.account.close)
            for session in self.sessions:
                stack.callback(session.close)
            if self.native_api:
                stack.callback(self.native_api.close)
            self.save_status(stopped=True)
