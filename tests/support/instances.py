"""Shared synthetic instances fixtures; contains no executable tests."""
from tests.support.model_support import default_config
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.support.synthetic import NOW, msg
from tests.support.pipeline import FakeJob
from wxbridge.app.coordinator import Coordinator
from wxbridge.config import load_config
from tests.support.paths import ROOT


CHATS = ('10001@chatroom', '10002@chatroom')


class InstanceFixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'config.json'
        self.data = dict(queue=dict(file=str(self.root/'queue.sqlite3')),
                         state_file=str(self.root/'state.json'), memory=dict(directory=str(self.root/'memory')),
                         provider_env=str(self.root/'provider.env'))
        models = default_config()['models']
        self.instance_data = [dict(id=name, chat=chat, enabled=True,
            system_prompt_file='system.md', models=deepcopy(models),
            target=dict(bot_name=name), search=dict(enabled=False),
            memory=dict(enabled=False, avatar=dict(enabled=False)))
            for name,chat in zip(('alpha','beta'),CHATS)]
        p = patch.dict(os.environ, {}, clear=True); p.start(); self.addCleanup(p.stop)

    def load(self, data=None):
        from wxbridge.instances import GROUP_FIELDS
        from wxbridge.config_merge import deep_merge
        root = deepcopy(self.data if data is None else data)
        common = {key:root.pop(key) for key in list(root) if key in GROUP_FIELDS and key not in ('queue','memory')}
        for section,key in (('queue','file'),('memory','directory')):
            value=root.get(section,{})
            common[section]={k:v for k,v in value.items() if k!=key}
            root[section]={key:value[key]} if key in value else {}
        self.path.write_text(json.dumps(root),encoding='utf-8')
        for entry in self.instance_data:
            directory=self.root/'instance'/entry['id']
            directory.mkdir(parents=True,exist_ok=True)
            body=deep_merge(common,{k:v for k,v in entry.items() if k!='id'})
            (directory/'config.json').write_text(json.dumps(body),encoding='utf-8')
            (directory/'system.md').write_text(entry['id']+' instructions',encoding='utf-8')
        return load_config(self.path)


class MultiGroupFixture(InstanceFixture):
    def setUp(self):
        super().setUp()
        self.data.update(memory=dict(enabled=False, directory=str(self.root/'memory')),
                         native_api=dict(enabled=True, allowed_chat_ids=list(CHATS)), ui=dict(backend='native_service', dry_run=False),
                         participation=dict(enabled=True, debounce_seconds=0), target=dict(cooldown_seconds=0),
                         queue=dict(file=str(self.root/'queue.sqlite3')),
                         account=dict(send_interval_seconds=0))
        self.cfg = self.load()
        self.cfg['poll_interval_seconds'] = 0
        for e in self.cfg['instances']:
            e['config']['poll_interval_seconds'] = 0
            Path(e['config']['queue']['file']).parent.mkdir(parents=True, mode=0o700)
        self.rows, self.now, self.jobs, self.decision_jobs = {chat: [] for chat in CHATS}, float(NOW), [], []
        self.store = Mock()
        self.store.self_wxid = 'wxid_bot'
        self.store.resolve_chat.side_effect = lambda chat: (CHATS[0] if chat == '旧群' else chat, '合成群')
        self.store.last_local_id.side_effect = lambda chat: max([m['local_id'] for m in self.rows[chat]], default=0)
        self.store.messages_after.side_effect = lambda chat, cursor, limit: [m for m in self.rows[chat] if m['local_id'] > cursor][:limit]
        self.store.messages_in_window.side_effect = lambda chat, start, end: [m for m in self.rows[chat] if start <= m['time'] <= end]
        from tests.support.synthetic import history_page
        self.store.messages_before.side_effect = lambda chat, **kw: history_page(self.rows[chat], **kw)
        for target, opts in [('wxbridge.storage.journal.checked_path', dict(side_effect=lambda v, **kw: Path(v))),
                             ('wxbridge.app.bot.time.time', dict(side_effect=lambda: self.now)),
                             ('wxbridge.native.server.NativeServer', {})]:
            p = patch(target, **opts); p.start(); self.addCleanup(p.stop)
        self.coordinator = self.make()
        self.addCleanup(lambda: self.coordinator.close())

    def make(self):
        coordinator = Coordinator(self.cfg, store=self.store)
        for session in coordinator.sessions:
            def factory(kind, payload, _id=session.instance_id, _chat=session.chat_id, **kwargs):
                job = FakeJob(kind, payload, **kwargs)
                job.owner = _id
                if kind == 'participation':
                    source_ids = payload[2]['source_ids']
                    rows = {m['local_id']: m for m in self.rows[_chat]}
                    selected = [identifier for identifier in source_ids if
                        self.store.self_wxid in (rows[identifier].get('mentioned_users') or [])
                        or (rows[identifier].get('pat') or {}).get('target') == self.store.self_wxid]
                    job.result = dict(ok=True, decision=dict(reply_to=selected))
                    self.decision_jobs.append(job)
                else:
                    self.jobs.append(job)
                return job
            session.job_factory = factory
        return coordinator

    def add(self, index, identifier, text='合成提问', sender='wxid_friend', mention=True):
        row = msg(identifier, int(self.now), text, sender)
        row['mentioned_users'] = ['wxid_bot'] if mention else []
        self.rows[CHATS[index]].append(row)
        return row

    def tick(self): self.coordinator.tick()

    def active(self, kind): return [j for j in self.jobs if j.kind == kind and not j.closed]

    def finish(self, owner, reply='合成回复'):
        for job in self.active('generate'):
            if job.owner == owner: job.result = dict(ok=True, reply=reply)
        self.tick()
