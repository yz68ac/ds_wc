"""Shared synthetic native_api fixtures; contains no executable tests."""
from __future__ import annotations
from tests.support.model_support import default_config
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from wxbridge.app.delivery import DeliveryController
from wxbridge.storage.journal import Journal
from tests.support.paths import ROOT


def payload(identifier='native-synthetic-1', text='中文第一行[偷笑]\n第二行😀', chat='filehelper'):
    return dict(op='send_text',request_id=identifier,chat_id=chat,text=text)


class JournalFixture(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'.test-tmp';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'queue.sqlite3'
        self.identity={'account':'synthetic','chat':'synthetic@chatroom'}
        self.journal=self.open();self.addCleanup(lambda:self.journal.close())
        self.cfg=default_config();self.cfg['ui']['dry_run']=False
        self.cfg['target']['cooldown_seconds']=0
        self.store=Mock(self_wxid='synthetic');self.store.last_local_id.return_value=2
        self.delivery=DeliveryController(self.cfg,self.journal,self.store,'synthetic@chatroom','合成群')

    def open(self):
        with patch('wxbridge.storage.journal.checked_path',side_effect=lambda p,**kw:Path(p)):
            return Journal(str(self.path),self.identity,88)

    def enqueue(self,value=None):
        return self.journal.enqueue_external(value or payload(),'File Transfer',100,
                                             max_pending=16,max_requests=1024)

    def normal_task(self,chunks=None,notice=False):
        self.cfg['ui']['backend']='native_service'
        with self.journal.db:
            identifier=self.journal._next_task_id()
            task=dict(id=identifier,phase='queued',version=1,sender='other',trigger_kind='mention',
                deadline=0,accepted_at=0,triggers=[],intent=None,blocked_reason='',generation='ready',
                next_chunk=0,chunks=chunks or ['合成回复'])
            if notice:
                self.cfg['search']['feedback_enabled']=True
                task['notice']=dict(state='pending',text='合成搜索提示')
            self.journal.put(task)
        return identifier
