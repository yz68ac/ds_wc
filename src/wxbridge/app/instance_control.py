"""Owner-loop instance lifecycle driven by explicit, revision-bound local requests.

The Web process writes metadata only. All readers, Journals, controllers and
model clients remain owned by Coordinator; no additional database writer exists.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from wxbridge.app.build_info import runtime_identity
from wxbridge.instances import INSTANCE_ID, runtime_locks

REQUEST_FILE = '.runtime-request.json'


def read_request(path):
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 4096:
        raise ValueError('invalid_instance_request')
    value=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value,dict) or set(value)!={'id','action','revision','created_at'}
            or not isinstance(value['id'],str) or not re.fullmatch('[a-f0-9]{32}',value['id'])
            or value['action'] not in {'start','stop','apply'}
            or not isinstance(value['revision'],str) or not re.fullmatch('[a-f0-9]{64}',value['revision'])
            or type(value['created_at']) not in (int,float)):
        raise ValueError('invalid_instance_request')
    return value


class InstanceControl:
    def __init__(self,owner):
        self.owner=owner
        self.path=Path(owner.cfg['_account_config_file'])
        self.directory=self.path.parent/'instance'
        self.initial_root=self.path.read_bytes()
        self.rows={entry['id']:dict(state='running' if entry['enabled'] else 'stopped',
                                   applied_revision=entry.get('config_revision'),request_id=None,error='')
                   for entry in owner.cfg['instances']}
        self.seen,self.pending,self.checked={}, {}, 0
        self.locks,self.locked=ExitStack(),set(runtime_locks(owner.cfg))
        # A process restart has already loaded the desired configuration. Old
        # request files must not force another client restart after recovery.
        for path in self.directory.glob('*/'+REQUEST_FILE):
            try:
                record=read_request(path)
            except (OSError,ValueError,TypeError):
                continue
            self.seen[path.parent.name]=record['id']
            if path.parent.name in self.rows:
                self.rows[path.parent.name]['request_id']=record['id']

    def _session(self,identifier):
        return next((item for item in self.owner.sessions if item.instance_id==identifier),None)

    def _inactive(self,identifier,path):
        from wxbridge.storage.journal import open_readonly
        if not Path(path).exists():
            return
        journal=open_readonly(str(path))
        if journal.get('identity',{}).get('account')!=self.owner.account.store.self_wxid:
            journal.close()
            raise ValueError('inactive_queue_account_mismatch')
        self.owner.account.inactive.append(SimpleNamespace(instance_id=identifier,journal=journal,ui_job=None))

    def _start(self,identifier):
        from wxbridge.config import load_config
        from wxbridge.app.runtime import instance_lock
        from wxbridge.app.bot import GroupSession
        if self.path.read_bytes()!=self.initial_root:
            raise ValueError('account_config_changed_restart_required')
        current=load_config(self.path)
        entry=next((entry for entry in current['instances'] if entry['id']==identifier),None)
        if entry is None or not entry['enabled']:
            raise ValueError('instance_not_enabled')
        cfg=deepcopy(entry['config'])
        cfg['ui']['dry_run']=self.owner.cfg['ui']['dry_run']
        cfg['ui']['verify_chat_selected']=True
        cfg['reply_to_backlog']=False
        if cfg['ui']['dry_run']:
            cfg['queue']['file']+='.dry-run'
            cfg['state_file']=str(Path(cfg['state_file']).parent/'dry-run/state.json')
        elif (cfg['ui']['backend']!='native_service'
              or cfg['vision']['download_enabled'] and cfg['vision']['download_backend']!='native'):
            raise ValueError('invalid_instance_transport')
        resolved,_=self.owner.account.store.resolve_chat(entry['chat'])
        if resolved!=entry['chat']:
            raise ValueError('instance_chat_identity_mismatch')
        if cfg['ui']['backend']=='native_service' and entry['chat'] not in cfg['native_api']['allowed_chat_ids']:
            raise ValueError('instance_target_not_allowed')
        lock=cfg['queue']['file']+'.lock'
        if lock not in self.locked:
            self.locks.enter_context(instance_lock(lock))
            self.locked.add(lock)
        removed=[item for item in self.owner.account.inactive if item.instance_id==identifier]
        for item in removed:
            item.journal.close()
            self.owner.account.inactive.remove(item)
        try:
            session=GroupSession(cfg,echo=self.owner.echo,store=self.owner.account.store,
                                 account=self.owner.account,instance_id=identifier,start_gateway=False)
        except BaseException:
            self._inactive(identifier,cfg['queue']['file'])
            raise
        self.owner.sessions.append(session)
        return entry['config_revision']

    def _request(self,identifier,record):
        config_path=self.directory/identifier/'config.json'
        if config_path.is_symlink() or hashlib.sha256(config_path.read_bytes()).hexdigest()!=record['revision']:
            raise ValueError('instance_configuration_changed')
        config=json.loads(config_path.read_text(encoding='utf-8-sig'))
        if (record['action']=='stop' and config.get('enabled',True)
                or record['action']=='start' and not config.get('enabled',True)):
            raise ValueError('instance_configuration_changed')
        row=self.rows.setdefault(identifier,dict(applied_revision=None))
        row.update(request_id=record['id'],error='')
        session=self._session(identifier)
        if session:
            if not session.quiescing:
                session.begin_stop()
            self.pending[identifier]=record
            row['state']='stopping'
        elif record['action']=='stop' or not config.get('enabled',True):
            row.update(state='stopped',applied_revision=record['revision'])
        else:
            row['applied_revision']=self._start(identifier)
            row['state']='running'

    def tick(self):
        now=time.monotonic()
        if now-self.checked>=1:
            self.checked=now
            for path in self.directory.glob('*/'+REQUEST_FILE):
                identifier=path.parent.name
                if not INSTANCE_ID.fullmatch(identifier):
                    continue
                try:
                    record=read_request(path)
                    if self.seen.get(identifier)==record['id']:
                        continue
                    self.seen[identifier]=record['id']
                    self.rows.setdefault(identifier,{})['request_id']=record['id']
                    self._request(identifier,record)
                except Exception as exc:
                    row=self.rows.setdefault(identifier,{})
                    row.update(state='error',error=self._error(exc))
        for identifier,record in list(self.pending.items()):
            session=self._session(identifier)
            if session and not session.drain_stop():
                continue
            self.pending.pop(identifier)
            # Cleanup failures retain the runtime's existing fail-closed rule.
            if session:
                path=session.cfg['queue']['file']
                session.close()
                self.owner.sessions.remove(session)
                self._inactive(identifier,path)
            try:
                # Recheck revision after waiting for a physical request to end.
                self._request(identifier,record)
            except Exception as exc:
                self.rows[identifier].update(state='error',error=self._error(exc))
        self._update_build()

    @staticmethod
    def _error(exc):
        allowed={'instance_configuration_changed','account_config_changed_restart_required',
                 'instance_chat_identity_mismatch','inactive_queue_account_mismatch','instance_not_enabled',
                 'instance_target_not_allowed'}
        return str(exc) if isinstance(exc,ValueError) and str(exc) in allowed else 'instance_transition_failed'

    def _update_build(self):
        if self.pending:
            return
        try:
            from wxbridge.config import load_config
            current=load_config(self.path)
            if self.path.read_bytes()!=self.initial_root:
                return
            for entry in current['instances']:
                row=self.rows.get(entry['id'],{})
                if row.get('applied_revision')!=entry['config_revision'] or row.get('state')!=('running' if entry['enabled'] else 'stopped'):
                    return
            self.owner.build=runtime_identity(current)
        except (OSError,ValueError,TypeError,KeyError):
            return

    def status(self):
        return {key:dict(value) for key,value in self.rows.items()}

    def close(self):
        self.locks.close()
