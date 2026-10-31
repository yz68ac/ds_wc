"""Multiple logical group sessions; all WeChat reads stay on this main loop."""
from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
import time

from wxbridge.native.protocol import settings as native_settings
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
        self.instance_control = None
        self.closed = False
        self.rotation, self.generation_rotation, self.delivery_rotation = 0, 0, 0
        self.started_at, self.build = time.time(), runtime_identity(cfg)
        self.status_path = Path(cfg['state_file']).parent/'account-status.json'
        self.status_signature, self.status_written_at = None, 0
        self.status_write_error = ''
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
            for identifier, active in configs:
                session = GroupSession(active, echo=echo, store=self.account.store, account=self.account,
                                       instance_id=identifier, start_gateway=False)
                self.sessions.append(session)
            options = native_settings(cfg)
            if options['enabled'] and not cfg['ui'].get('dry_run'):
                from wxbridge.native.server import NativeServer
                from wxbridge.app.native_gateway import AccountNativeService
                self.native_api = NativeServer(options, AccountNativeService(self, options))
            if cfg.get('_account_config_file'):
                from wxbridge.app.instance_control import InstanceControl
                self.instance_control = InstanceControl(self)
        except BaseException:
            self.close()
            raise

    def tick(self):
        if self.instance_control:
            self.instance_control.tick()
        active = [s for s in self.sessions if not getattr(s,'quiescing',False)]
        order = active[self.rotation:]+active[:self.rotation]
        self.rotation = (self.rotation+1) % max(1,len(active))
        # Intake all groups before giving foreground work any API slots.
        for session in order:
            session.intake()
        if self.native_api:
            self.native_api.tick()
        for session in order:
            session.collect_results()
        for session in order:
            session.prioritize()
        for session in order:
            session.start_foreground()
        for session in order:
            session.participate()
        send_order = active[self.delivery_rotation:]+active[:self.delivery_rotation]
        for session in send_order:
            free = self.account.sending() is None
            session.deliver()
            if free and self.account.sending() is not None:
                self.delivery_rotation = (active.index(session)+1) % max(1,len(active))
                break
        for session in order:
            session.background()
            session.maintain()
        self.save_status()

    def save_status(self, *, stopped=False):
        status = dict(schema=1, pid=os.getpid(), updated_at=time.time(), started_at=self.started_at,
                      phase='stopped' if stopped else 'listening', build=self.build,
                      **self.account.status(), instances={})
        status['features'] = dict(instance_control=self.instance_control is not None)
        status['instance_lifecycle'] = self.instance_control.status() if self.instance_control else {}
        counts = [s.worker_counts() for s in self.sessions]
        status['generation_workers'] = sum(c['generation'] for c in counts)
        status['memory'] = dict(running=any(c['memory'] for c in counts))
        status['participation'] = dict(outcome='evaluating' if any(c['participation'] for c in counts) else 'idle')
        status['inactive_queues'] = {s.instance_id: dict(queue_length=s.journal.pending_count(),
            unconfirmed=s.journal.has_intent(), cursor=s.journal.get('cursor',0),
            last_success=s.journal.get('last_success',0)) for s in self.account.inactive}
        status['blocked_reason'] = 'account_send_unconfirmed' if status['send_blocked'] else ''
        for session in self.sessions:
            value = session.journal.status()
            status['instances'][session.instance_id] = dict(
                queue_length=value['queue_length'], cursor=value['cursor'],
                blocked_reason=value.get('blocked_reason', ''), last_success=value['last_success'],
                model_workers=sum(session.worker_counts().values()), model_channels=session.models.status(),
                participation=value['participation'],
                context=session.journal.get('context', {}),
                context_policy={key:session.cfg['target'][key] for key in ('context_recent_messages',
                    'context_max_age_seconds','context_bot_messages','context_max_chars','context_budget_strategy')},
                profile=session.profile.status() if session.profile else dict(enabled=False),
                image_generation=dict(enabled=session.cfg['image_generation']['enabled'],
                    running=len(session.image_generation.jobs), last=value.get('last_image_generation')),
                models={role:value['model'] for role,value in session.cfg['models'].items()},
                quiescing=getattr(session,'quiescing',False),
                status_write_error=session.status_write_error)
        signature = json.dumps({k:v for k,v in status.items() if k != 'updated_at'}, sort_keys=True)
        clock = time.monotonic()
        if not stopped and signature == self.status_signature and clock-self.status_written_at < 2:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.status_path.with_suffix('.tmp')
            with open(temporary, 'w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(status, stream, ensure_ascii=False)
            os.replace(temporary, self.status_path)
        except OSError as exc:
            code = type(exc).__name__
            if code != self.status_write_error:
                log.warning('账户状态快照写入失败 category=%s', code)
            self.status_write_error = code
            return False
        self.status_write_error = ''
        self.status_signature, self.status_written_at = signature, clock
        return True

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
            if self.instance_control:
                stack.callback(self.instance_control.close)
            stack.callback(self.account.close)
            for session in self.sessions:
                stack.callback(session.close)
            if self.native_api:
                stack.callback(self.native_api.close)
            self.save_status(stopped=True)
