"""重启恢复的元数据边界；不读真实配置/聊天、不连接服务器、不调用模型。"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('boot_recovery',Path(__file__).resolve().parents[1]/'scripts/boot-recovery.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


class BootRecoveryTests(unittest.TestCase):
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
            db.execute('INSERT INTO tasks VALUES (?)',(json.dumps(dict(phase='queued',text='CHAT_SECRET',prompt='PROMPT_SECRET')),))
            db.execute('INSERT INTO memory_jobs VALUES (?)',('"MEMORY_SECRET"',))
            db.commit(); db.close()
            with patch.object(boot,'RUN',run),patch.object(boot,'PERSIST',persist):
                result=boot.checkpoint()
            text=(persist/'cursor.json').read_text()
            self.assertEqual(result,dict(saved=True,cursor=9,pending=1,memory_pending=1))
            for secret in ('KEY_SECRET','CHAT_SECRET','PROMPT_SECRET','MEMORY_SECRET'):
                self.assertNotIn(secret,text)
            self.assertEqual(json.loads(text)['identity'],identity)

    def test_missing_queue_does_not_overwrite_checkpoint(self):
        parent=Path(__file__).resolve().parents[1]/'.test-tmp'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name,patch.object(boot,'RUN',Path(name)):
            self.assertFalse(boot.checkpoint()['saved'])

    def test_live_listener_is_not_reopened_or_rescanned(self):
        with patch.object(boot,'validate_runtime'),patch.object(boot,'service_active',return_value=True),\
             patch.object(boot,'process_token') as token,patch.object(boot,'as_user') as run,patch.object(boot,'safe_status') as status:
            run.return_value.returncode=0
            boot.recover()
            token.assert_not_called()
            status.assert_called_once_with('listening')
            with self.assertRaises(ValueError):
                boot.recover(verify=True)
