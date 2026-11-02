"""Account-scoped, in-memory directory search; the Web never opens a database."""
import difflib
import json
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from wxbridge.instances import CHAT_ID
from .repository import ManagementError

def normalized(value):return ''.join(unicodedata.normalize('NFKC',value).casefold().split())

def match_score(query,values):
    query=normalized(query)
    if not query:return 0
    scores=[]
    for value in values:
        value=normalized(value)
        if not value:continue
        if query==value:scores.append(1)
        elif value.startswith(query):scores.append(.95)
        elif query in value:scores.append(.85)
        elif len(query)>=2:
            ratio=difflib.SequenceMatcher(None,query,value).ratio()
            if ratio>=.6:scores.append(ratio*.7)
    return max(scores,default=-1)

class ContactDirectory:
    def __init__(self,repository,*,demo=False):
        self.repository=repository;self.demo=demo
        self.lock=threading.Lock();self.key=None;self.updated=0;self.data=None

    def _load(self,refresh):
        cfg=self.repository.account()
        def stamp(path):
            try:s=Path(path).stat();return s.st_ino,s.st_mtime_ns
            except OSError:return None
        key=(cfg.db_dir,cfg.keys_file,stamp(cfg.keys_file),stamp(Path(cfg.state_file).parent/'wechat.pid'))
        if not refresh and self.key==key and self.data is None and time.monotonic()-self.updated<3:
            raise ManagementError('contacts_unavailable','联系人尚未就绪，请稍后刷新，或暂用手动 ID 创建。',409)
        if not refresh and self.key==key and self.data is not None and time.monotonic()-self.updated<30:return self.data
        if self.demo:
            data=dict(items=[dict(id='wxid_synthetic_friend',nickname='测试好友',remark='小林',last_active=2),
                             dict(id='123456789@chatroom',nickname='开发讨论群',remark='',last_active=3),
                             dict(id='wxid_new_friend',nickname='小明',remark='项目同事',last_active=1)],truncated=False)
        else:
            try:
                result=subprocess.run([sys.executable,'-m','wxbridge.wechat.directory','--config',str(self.repository.config_file)],
                                      capture_output=True,text=True,encoding='utf-8',timeout=12)
                if result.returncode:raise ValueError()
                if len(result.stdout)>16*1024*1024:raise ValueError()
                data=json.loads(result.stdout)
                if not isinstance(data.get('items'),list):raise ValueError()
            except (OSError,ValueError,subprocess.TimeoutExpired):
                self.key=key;self.data=None;self.updated=time.monotonic()
                raise ManagementError('contacts_unavailable','联系人暂不可用，请先完成微信登录和账号绑定；若尚未准备数据库密钥，请先完成账号服务的数据库准备。可暂用手动 ID 创建。',409) from None
        self.data=data;self.key=key;self.updated=time.monotonic()
        return data

    def search(self,query='',kind='all',refresh=False):
        if not isinstance(query,str) or len(query)>128 or kind not in ('all','group','private'):
            raise ManagementError('invalid_contact_query','搜索条件无效。')
        with self.lock:data=self._load(refresh)
        assigned={item['chat']:item['id'] for item in self.repository.list()}
        rows=[];seen=set()
        for raw in data['items']:
            uid=raw.get('id','')
            if not isinstance(uid,str) or not CHAT_ID.fullmatch(uid) or uid in seen:continue
            seen.add(uid)
            category='group' if uid.endswith('@chatroom') else 'private'
            if kind!='all' and category!=kind:continue
            nickname=str(raw.get('nickname',''))[:256];remark=str(raw.get('remark',''))[:256]
            score=match_score(query,[uid,nickname,remark])
            if score<0:continue
            rows.append(dict(id=uid,name=remark or nickname or uid,nickname=nickname,remark=remark,kind=category,
                             instance_id=assigned.get(uid),_score=score,_time=raw.get('last_active',0)))
        rows.sort(key=lambda item:(-item['_score'],-item['_time'],item['name'],item['id']))
        total=len(rows)
        return dict(items=[{k:v for k,v in item.items() if not k.startswith('_')} for item in rows[:100]],
                    total=total,truncated=bool(data.get('truncated')) or total>100)
