"""重启恢复的元数据边界；不读真实配置/聊天、不连接服务器、不调用模型。"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support.boot import boot


class BootRecoveryTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        application = patch.object(boot, 'APP', Path(temporary.name))
        application.start()
        self.addCleanup(application.stop)

    def test_restore_only_known_account_group_and_nonfuture_cursor(self):
        identity = dict(account='synthetic_bot',chat='synthetic@chatroom')
        value = dict(schema=1,identity=identity,cursor=7)
        state = boot.restored_state(value,identity,10)
        self.assertEqual(state,dict(chats={'synthetic@chatroom':dict(last_local_id=7)}))
        for changed in [dict(value,schema=2),dict(value,identity=dict(identity,account='other')),
                        dict(value,identity=dict(identity,chat='other@chatroom')),dict(value,cursor=11),
                        dict(value,cursor=True),dict(value,cursor=-1)]:
            with self.assertRaises(ValueError):
                boot.restored_state(changed,identity,10)

    def test_checkpoint_contains_no_task_body_prompt_or_keys(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name:
            run=Path(name)/'run'; persist=Path(name)/'persist'
            run.mkdir(); persist.mkdir()
            db=sqlite3.connect(run/'queue.sqlite3')
            db.executescript('CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE tasks(data TEXT); CREATE TABLE memory_jobs(data TEXT);')
            identity=dict(account='synthetic_bot',chat='synthetic@chatroom')
            db.executemany('INSERT INTO meta VALUES (?,?)',[('identity',json.dumps(identity)),('cursor','9'),('secret','"KEY_SECRET"')])
            db.execute('INSERT INTO tasks VALUES (?)',(json.dumps(dict(phase='queued',intent=None,text='CHAT_SECRET',prompt='PROMPT_SECRET')),))
            db.execute('INSERT INTO memory_jobs VALUES (?)',('"MEMORY_SECRET"',))
            db.commit(); db.close()
            account=SimpleNamespace(pw_uid=995,pw_gid=984)
            with patch.object(boot,'RUN',run),patch.object(boot,'PERSIST',persist),\
                 patch.object(boot,'configured_groups',return_value=[(None,dict(queue=dict(file=str(run/'queue.sqlite3'))),True)]),\
                 patch.object(boot,'owner',return_value=account),\
                 patch.object(boot.os,'geteuid',return_value=0,create=True),\
                 patch.object(boot.os,'chown',create=True) as ownership:
                result=boot.checkpoint()
            self.assertEqual(ownership.call_count,1)
            self.assertEqual(ownership.call_args.args[1:],(995,984))
            text=(persist/'cursor.json').read_text()
            self.assertEqual(result,dict(saved=True,cursor=9,pending=1,memory_pending=1))
            for secret in ('KEY_SECRET','CHAT_SECRET','PROMPT_SECRET','MEMORY_SECRET'):
                self.assertNotIn(secret,text)
            self.assertEqual(json.loads(text)['identity'],identity)

    def test_missing_queue_does_not_overwrite_checkpoint(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name,patch.object(boot,'RUN',Path(name)),\
             patch.object(boot,'configured_groups',return_value=[(None,dict(queue=dict(file=str(Path(name)/'queue.sqlite3'))),True)]):
            self.assertFalse(boot.checkpoint()['saved'])

    def test_busy_database_stops_preparation_before_scanning_or_probing(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name:
            directory=Path(name);settings=directory/'settings.json';settings.write_text('{}')
            with patch.object(boot,'RUN',directory),patch.object(boot,'SETTINGS',settings),\
                 patch.object(boot,'validate_runtime'),patch.object(boot,'process_token',return_value={'pid':123}),\
                 patch.object(boot,'database_locks',side_effect=BlockingIOError),patch.object(boot.subprocess,'run') as run:
                with self.assertRaises(BlockingIOError):boot.prepare()
                run.assert_not_called()
