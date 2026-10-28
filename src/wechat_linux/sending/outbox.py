"""Private durable intents. Unknown submissions are reconciled, never re-submitted."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from contextlib import closing

from wechat_linux.errors import NativeError
from wechat_linux.models import DeliveryState, SendReceipt
from wechat_linux.native.protocol import validate_send
from wechat_linux.runtime.memoryfs import require_memory_directory
from wechat_linux.runtime.locking import instance_lock
from wechat_linux.sending.confirmation import scan_confirmation


class ReliableSender:
    APPLICATION_ID = 0x57434C31

    def __init__(self,path,store,submitter,allowed_chats,*,max_history=4096):
        if type(max_history) is not int or not 1<=max_history<=4096:raise NativeError('invalid_history_limit')
        if not isinstance(store.self_wxid,str) or not store.self_wxid:raise NativeError('account_not_ready')
        self.store,self.submitter,self.allowed=store,submitter,frozenset(allowed_chats)
        self.max_history=max_history
        target=Path(path).absolute();require_memory_directory(str(target.parent))
        if target.is_symlink():raise NativeError('unsafe_outbox')
        self.db=None
        self._lock=instance_lock(str(target)+'.lock')
        self._lock.__enter__()
        try:
            fd=os.open(target,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            try:
                s=os.fstat(fd)
                if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or s.st_mode&0o077:raise NativeError('unsafe_outbox')
                empty=s.st_size==0
            finally:os.close(fd)
            if not empty:
                # Inspect foreign files read-only: even journal recovery must not modify them.
                with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as check:
                    marker=check.execute('PRAGMA application_id').fetchone()[0]
                    version=check.execute('PRAGMA user_version').fetchone()[0]
                    if marker!=self.APPLICATION_ID or version!=1:raise NativeError('foreign_outbox')
                    account=check.execute('SELECT account FROM identity').fetchall()
                    if account!=[(store.self_wxid,)]:raise NativeError('outbox_account_mismatch')
            self.db=sqlite3.connect(target)
            self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA temp_store=MEMORY')
            if empty:
                with self.db:
                    self.db.execute('CREATE TABLE identity(account TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE intents(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,data TEXT NOT NULL)')
                    self.db.execute('INSERT INTO identity VALUES (?)',(store.self_wxid,))
                    self.db.execute(f'PRAGMA application_id={self.APPLICATION_ID}')
                    self.db.execute('PRAGMA user_version=1')
        except BaseException:
            self.close()
            raise

    def _row(self,request_id):
        row=self.db.execute('SELECT fingerprint,data FROM intents WHERE request_id=?',(request_id,)).fetchone()
        return (row[0],json.loads(row[1])) if row else None

    def _put(self,data):
        self.db.execute('UPDATE intents SET data=? WHERE request_id=?',(json.dumps(data,ensure_ascii=False),data['request_id']))

    @staticmethod
    def receipt(data):
        return SendReceipt(data['request_id'],data['chat_id'],DeliveryState(data['state']),data.get('ack_id'),data.get('error',''))

    def send_text(self,chat_id,text,request_id):
        validate_send(dict(op='send_text',chat_id=chat_id,text=text,request_id=request_id))
        if chat_id not in self.allowed:raise NativeError('chat_not_allowed')
        fingerprint=hashlib.sha256(json.dumps([chat_id,text],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old=self._row(request_id)
            if old:
                if old[0]!=fingerprint:raise NativeError('request_id_conflict')
                return self.receipt(old[1])
            rows=self.db.execute('SELECT data FROM intents').fetchall()
            if any(json.loads(row[0])['state'] in ('unknown','submitted') for row in rows):raise NativeError('delivery_busy')
            if len(rows)>=self.max_history:raise NativeError('request_history_full')
            baseline=self.store.last_local_id(chat_id)
            data=dict(request_id=request_id,chat_id=chat_id,text=text,state='unknown',baseline=baseline,
                      created_at=time.time(),deadline=time.time()+10,error='')
            self.db.execute('INSERT INTO intents VALUES (?,?,?)',(request_id,fingerprint,json.dumps(data,ensure_ascii=False)))
        try:
            metadata=self.submitter(chat_id,text,request_id)
            submitted=(metadata.get('operation') or {}).get('submitted')
            data['state']='submitted' if submitted is True else 'not_submitted' if submitted is False else 'unknown'
        except Exception as exc:
            submitted=getattr(exc,'submitted',None)
            if submitted is False:data['state']='not_submitted'
            code=getattr(exc,'code','submission_unknown')
            data['error']=code if isinstance(code,str) and re.fullmatch('[a-z_]{1,64}',code) else 'submission_unknown'
        if data['state']=='not_submitted':data['text']=''
        with self.db:self._put(data)
        return self.receipt(data)

    def get_status(self,request_id):
        row=self._row(request_id)
        if not row:raise NativeError('request_not_found')
        return self.receipt(row[1])

    def confirm(self,request_id):
        row=self._row(request_id)
        if not row:raise NativeError('request_not_found')
        data=row[1]
        if data['chat_id'] not in self.allowed:raise NativeError('chat_not_allowed')
        if data['state'] not in ('unknown','submitted'):return self.receipt(data)
        now=time.time();ack=scan_confirmation(self.store,data['chat_id'],self.store.self_wxid,data['text'],data,now,
                                               blocked=now>=data['deadline'])
        if ack is not None:data.update(state='confirmed',ack_id=ack,text='',error='')
        elif now>=data['deadline']:data.update(state='unknown',error='send_confirmation_timeout')
        with self.db:self._put(data)
        return self.receipt(data)

    def close(self):
        try:
            if self.db is not None:self.db.close();self.db=None
        finally:
            if self._lock is not None:
                lock,self._lock=self._lock,None;lock.__exit__(None,None,None)
