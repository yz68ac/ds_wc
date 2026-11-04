"""Fixed onboarding operations for the account's authenticated local console."""
import json
import subprocess
from pathlib import Path

from .system import Paths, as_user, atomic_json, private_json


def login_snapshot(root):
    paths=Paths(app=Path(root))
    code = '''import base64,io,json,qrcode
from wxbridge.host.login import LoginRuntime
r=LoginRuntime()
with r.locked():
    status=r.viewer_snapshot()
    r.save(r.state(),status)
    result=dict(phase=status.phase,qr='')
    if status.phase=='qr_required':
        image=qrcode.make(r.controller.read_qr().payload)
        buffer=io.BytesIO(); image.save(buffer,format='PNG')
        result['qr']='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()
print(json.dumps(result))
'''
    try:
        result=as_user(paths,[str(paths.code/'.venv/bin/python'),'-c',code],capture=True,timeout=12)
    except subprocess.TimeoutExpired:
        raise ValueError('login_not_ready') from None
    if result.returncode:raise ValueError('login_not_ready')
    value=json.loads(result.stdout)
    if len(value.get('qr',''))>48000:raise ValueError('login_qr_too_large')
    return value


def bind_configured_account(supervisor):
    from wxbridge.instances import CHAT_ID
    from .binding import bind_account
    from wxbridge.control import BOT, HELPER, RECOVERY
    if supervisor.enabled or any(supervisor.children[n].process is not None for n in (BOT,RECOVERY)):
        raise ValueError('stop_bot_before_account_binding')
    if supervisor.children[HELPER].stopping:
        raise ValueError('helper_restarting')
    paths=Paths(app=supervisor.root)
    from .binding import configuration_lock
    with configuration_lock(paths):
        chats=set()
        for file in (supervisor.root/'instance').glob('*/config.json'):
            chat=private_json(file).get('chat')
            if not isinstance(chat,str) or not CHAT_ID.fullmatch(chat):raise ValueError('invalid_chat')
            chats.add(chat)
        helper_path=Path('/etc/wxbridge/helper-config.json')
        helper=private_json(helper_path)
        allowed=sorted(set(helper['allowed_chat_ids'])|chats)
        if len(allowed)>16:raise ValueError('too_many_authorized_chats')
        result=bind_account(paths,current_state=supervisor.status())
        from wxbridge.config.compatibility import normalize_transport_config
        config=normalize_transport_config(private_json(supervisor.root/'config.json'))
        config['helper_transport']['allowed_chat_ids']=allowed
        atomic_json(supervisor.root/'config.json',config,uid=supervisor.account.pw_uid,gid=supervisor.account.pw_gid)
        if helper['allowed_chat_ids']!=allowed:
            helper['allowed_chat_ids']=allowed
            atomic_json(helper_path,helper)
            supervisor.children[HELPER].stop()
    # A fresh account must be searchable before its first business instance exists.
    supervisor.children[RECOVERY].command[-1]='directory'
    supervisor.start(RECOVERY,capture=True)
    return dict(bound=result['bound'],authorized_count=len(chats),
                message='账号及实例会话授权已保存；实例启停状态保持原样。')
