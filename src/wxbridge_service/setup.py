"""Bind a fresh installation to its logged-in account without reading chat bodies."""
import json
import os
from pathlib import Path
import signal
import time
import uuid

from .host import atomic_json, atomic_file, as_user, need_root, private_json, service_account
from wxbridge_web.control import request as control_request


def discover_database(paths, account):
    pid = int((paths.runtime/'wechat.pid').read_text())
    proc = Path('/proc')/str(pid)
    if proc.stat().st_uid != account.pw_uid or (proc/'exe').resolve() != Path('/opt/wechat/wechat'):
        raise ValueError('当前微信进程身份不匹配。')
    found = set()
    for fd in (proc/'fd').iterdir():
        try:
            target = fd.resolve(strict=True)
        except OSError:
            continue
        if target.suffix != '.db':
            continue
        found.update(parent for parent in target.parents if parent.name == 'db_storage'
                     and parent.is_relative_to(Path(account.pw_dir)))
    if len(found) != 1:
        raise ValueError('尚未发现唯一的账号数据库目录，请先完成微信登录。')
    return found.pop()


def checked_database(path, account):
    value = Path(path).expanduser().resolve(strict=True)
    if (value.name != 'db_storage' or not value.is_dir()
            or not value.is_relative_to(Path(account.pw_dir).resolve())
            or value.stat().st_uid != account.pw_uid):
        raise ValueError('数据库目录必须是 wxbridge 用户 HOME 内、归该用户所有的 db_storage。')
    return value


def restart_sender(state):
    if not state or state['components']['sender']['state'] != 'active':
        return
    pid = state['components']['sender']['pid']
    proc = Path('/proc')/str(pid)
    args = (proc/'cmdline').read_bytes().split(b'\0')
    if b'/opt/wxbridge-native-helper/helper.py' not in args:
        raise ValueError('发送 helper 进程身份不匹配。')
    if (proc/'task'/str(pid)/'children').read_text().strip():
        raise ValueError('发送 helper 仍有作业，请等待完成后重试绑定。')
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        now = control_request('status')['components']['sender']
        if now['state'] == 'active' and now['pid'] != pid:
            return
        time.sleep(.25)
    raise ValueError('发送组件正在恢复，请检查 wxbridge status。')


def bind_account(paths, db_dir='auto', *, chat=None, identifier='default'):
    need_root()
    from wxbridge.instances import CHAT_ID, INSTANCE_ID
    if not INSTANCE_ID.fullmatch(identifier) or chat is not None and not CHAT_ID.fullmatch(chat):
        raise ValueError('实例 ID 或聊天 ID 无效。')
    try:
        state = control_request('status')
    except (FileNotFoundError, ConnectionRefusedError):
        state = None
    if state and (state['bot_enabled'] or state['components']['bot']['state'] != 'inactive'):
        raise ValueError('绑定前请先运行 wxbridge stop --wait，保留微信登录。')
    account = service_account()
    database = checked_database(discover_database(paths, account) if db_dir == 'auto' else db_dir, account)
    config_path, helper_path = paths.app/'config.json', paths.helper/'helper-config.json'
    config, helper = private_json(config_path), private_json(helper_path)
    if helper.get('uid') != account.pw_uid:
        raise ValueError('发送 helper 与服务用户绑定不一致。')
    original = {p:p.read_bytes() for p in (config_path, helper_path, paths.settings)}
    settings = private_json(paths.settings)
    config['db_dir'], settings['db_dir'] = str(database), str(database)
    instance_path = paths.app/'instance'/identifier
    existing_instance = (instance_path/'config.json').exists()
    if chat:
        if existing_instance and private_json(instance_path/'config.json').get('chat') != chat:
            raise ValueError('该实例已绑定其他会话，请使用新的实例 ID。')
        for file in (paths.app/'instance').glob('*/config.json'):
            if file.parent != instance_path and private_json(file).get('chat') == chat:
                raise ValueError('该会话已绑定其他实例。')
        for values in (config['native_api']['allowed_chat_ids'], helper['allowed_chat_ids']):
            if chat not in values:
                values.append(chat)
            if len(values) > 16:
                raise ValueError('发送授权清单最多支持16个会话。')
    backup = paths.home/'setup-backups'/str(uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    for index, (path, raw) in enumerate(original.items()):
        atomic_file(backup/(str(index)+'.json'), raw)
    atomic_json(backup/'manifest.json', {str(p):str(index)+'.json' for index,p in enumerate(original)})
    try:
        atomic_json(config_path, config, uid=account.pw_uid, gid=account.pw_gid)
        atomic_json(paths.settings, settings)
        atomic_json(helper_path, helper)
        if chat and not existing_instance:
            template = dict(enabled=False, chat=chat, system_prompt_file='system.md',
                            participation=dict(enabled=True), memory=dict(enabled=False, avatar=dict(enabled=False)),
                            profile=dict(enabled=False), image_generation=dict(enabled=False),search=dict(enabled=False))
            code = ("import json,sys; from wxbridge_web.repository import ConfigRepository; "
                    "r=ConfigRepository(sys.argv[1]); r.save(sys.argv[2],json.loads(sys.argv[3]),None,create=True)")
            result = as_user(paths, [str(paths.app/'.venv/bin/python'),'-c',code,str(paths.app),identifier,json.dumps(template)],
                             capture=True, timeout=15)
            if result.returncode:
                raise ValueError('实例创建失败，账号绑定已回退。')
            atomic_file(instance_path/'system.md', '你正在微信里和朋友聊天。自然、简洁地回应，保持身份清晰。\n'.encode(),
                        uid=account.pw_uid, gid=account.pw_gid)
        if helper != json.loads(original[helper_path]):
            restart_sender(state)
    except BaseException:
        for path, raw in original.items():
            owner = dict(uid=account.pw_uid, gid=account.pw_gid) if path == config_path else {}
            atomic_file(path, raw, **owner)
        raise
    return dict(bound=True, db_dir=str(database), instance=identifier if chat else None,
                instance_created=bool(chat and not existing_instance), bot_started=False,
                next='在 provider .env 填写密钥；管理台选择回复模型（群聊还需参与模型）并启用实例，然后运行 wxbridge start。')
