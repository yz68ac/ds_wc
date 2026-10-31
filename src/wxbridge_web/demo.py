"""Explicit synthetic preview, with no real account, credentials, database or systemd calls."""
import json
import os
from pathlib import Path
import threading
import time

from wxbridge_web.repository import ConfigRepository
from wxbridge_web.services import BOT, SESSION, HELPER, RECOVERY, WEB, COMPONENTS

DEMO_PASSWORD = 'wxbridge-demo-local'


def seed(root):
    root = Path(root)
    runtime = root/'runtime'
    runtime.mkdir(parents=True,exist_ok=True)
    account = dict(state_file=str(runtime/'state.json'),log_file=str(runtime/'bot.log'),
                   queue=dict(file=str(runtime/'queue.sqlite3')),provider_env=str(root/'provider.env'))
    (root/'config.json').write_text(json.dumps(account),encoding='utf-8')
    (root/'provider.env').write_text('\n'.join(name+'_API_KEY=synthetic-demo-not-a-real-key' for name in ['ANTHROPIC','DEEPSEEK','OPENAI']),encoding='utf-8')
    from provider.config import load_config
    from provider.catalog import make_entry,save_catalog
    from provider.discovery import DiscoveredModel
    config=load_config(root/'provider.env',environ={})
    examples={'anthropic':['claude-haiku-5-5','claude-sonnet-5-5'],
              'deepseek':['deepseek-flash','deepseek-v4-pro'],'openai':['gpt-4.1','gpt-image-1']}
    save_catalog(root/'models.json',{name:make_entry(config,name,[DiscoveredModel(name,model,model) for model in models]) for name,models in examples.items()})
    for name, chat, enabled in [('private_demo','wxid_synthetic_friend',True),('group_demo','123456789@chatroom',False)]:
        path=root/'instance'/name
        path.mkdir(parents=True)
        data=dict(enabled=enabled,chat=chat,system_prompt_file='system.md',
                  model_clients=dict(reply='anthropic',image='openai',background='deepseek'),
                  models=dict(reply=dict(model='claude-haiku-5-5',options=dict(max_tokens=1200,timeout_seconds=60,thinking='enabled',reasoning_effort='high')),
                              participation=dict(model='deepseek-flash')),
                  participation=dict(enabled=True),memory=dict(enabled=False),profile=dict(enabled=False))
        (path/'config.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        (path/'system.md').write_text('你正在微信里和朋友聊天。自然接话，日常回复一两句，答完就停。\n',encoding='utf-8')
    (runtime/'login-status.json').write_text(json.dumps(dict(phase='logged_in')),encoding='utf-8')
    (runtime/'bot.log').write_text('10-30 09:20:15 INFO 停止监听\n10-30 09:18:30 INFO 发送确认 task=12 local_id=36\n',encoding='utf-8')


class DemoServices:
    allow_control = True

    def __init__(self,root):
        self.root,self.active,self.applied,self.lock=Path(root),False,None,threading.RLock()
        self.applied_instances,self.lifecycle,self.seen={}, {}, {}

    def _apply(self,item,request_id=None):
        self.lifecycle[item['id']]=dict(state='running' if item['enabled'] else 'stopped',
            applied_revision=item['revision'],request_id=request_id,error='')
        if item['enabled']:
            data=ConfigRepository(self.root).get(item['id'])
            self.applied_instances[item['id']]=dict(revision=item['revision'],
                models={role:settings['model'] for role,settings in data['effective']['models'].items()})
        else:
            self.applied_instances.pop(item['id'],None)

    def status(self):
        with self.lock:
            now=time.time()
            items=ConfigRepository(self.root).list()
            if self.active:
                from wxbridge.app.instance_control import REQUEST_FILE,read_request
                for item in items:
                    path=self.root/'instance'/item['id']/REQUEST_FILE
                    if path.is_file():
                        record=read_request(path)
                        if self.seen.get(item['id'])!=record['id']:
                            self.seen[item['id']]=record['id']
                            if record['revision']==item['revision']:
                                self._apply(item,record['id'])
                if all(self.lifecycle.get(item['id'],{}).get('applied_revision')==item['revision'] for item in items):
                    self.applied=ConfigRepository(self.root).config_hash()
            entries={item['id']:dict(queue_length=0,model_workers=0,cursor=36,
                models=self.applied_instances[item['id']]['models'],
                model_channels={name:dict(running=False,queued=0,client_created=False) for name in ['reply','image','background']})
                for item in items if item['id'] in self.applied_instances}
            value=dict(schema=1,pid=os.getpid(),updated_at=now,phase='listening' if self.active else 'stopped',
                       build=dict(config_hash=self.applied),send_blocked=False,instances=entries,
                       features=dict(instance_control=True),instance_lifecycle=self.lifecycle)
            (self.root/'runtime/account-status.json').write_text(json.dumps(value),encoding='utf-8')
            states={name:('active' if name in {SESSION,HELPER,WEB} or (self.active and name in {BOT,RECOVERY}) else 'inactive') for name in COMPONENTS}
            return {name:dict(state=states[name],pid=os.getpid() if states[name]=='active' and name!=RECOVERY else 0) for name in COMPONENTS}

    def perform(self,action):
        with self.lock:
            if action=='apply' and not self.active:
                return dict(applied=False,message='演示：配置将在下次启动时加载。')
            self.active=action!='stop'
            if self.active:
                for item in ConfigRepository(self.root).list():
                    self._apply(item)
                self.applied=ConfigRepository(self.root).config_hash()
            return dict(applied=True,message={'start':'演示机器人已启动。','stop':'演示机器人已停止。','apply':'演示配置已应用。'}[action])
