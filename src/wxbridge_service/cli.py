"""wxbridge host CLI. Ordinary start/stop controls only the bot, preserving WeChat."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .host import Paths, UNIT, as_user, need_root, private_json
from wxbridge_web.control import request as control_request


def parser():
    p = argparse.ArgumentParser(prog='wxbridge', description='微信机器人主机管理；start/stop/restart 保留微信会话。')
    from wxbridge import __version__
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--root', type=Path, help='账号配置与 instance 所在目录；默认 /opt/wxbridge')
    commands = p.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', help='查看服务、登录和各实例的安全状态')
    status.add_argument('--json', action='store_true')
    for name, help_text in (('start','启动/恢复机器人'), ('stop','暂停机器人，保留微信和管理台'),
                            ('restart','重启正在运行的机器人；已暂停时保持暂停'), ('apply','应用配置，保留微信登录')):
        item = commands.add_parser(name, help=help_text)
        item.add_argument('--wait', action='store_true', help='等待实际状态变化')
        item.add_argument('--timeout', type=int, default=360, choices=range(1,601), metavar='1..600')
    service = commands.add_parser('service', help='管理整个主机服务；停止/重启会同时停止微信')
    service.add_argument('action', choices=('start','stop','restart','enable','disable','status'))
    login = commands.add_parser('login', help='在 SSH 终端查看二维码；Ctrl+C 只退出查看')
    login.add_argument('--status', action='store_true')
    login.add_argument('--vnc', action='store_true', help='临时 VNC，仅监听本机 5900；需安装时 --with-vnc')
    logs = commands.add_parser('logs', help='查看运行日志')
    logs.add_argument('--component', choices=('bot','wechat','web','sender','service'), default='bot')
    logs.add_argument('--lines', type=int, choices=range(1,1001), default=50, metavar='1..1000')
    logs.add_argument('-f','--follow', action='store_true')
    instance = commands.add_parser('instance', help='控制单个会话实例')
    instance.add_argument('action', choices=('list','start','stop','apply'))
    instance.add_argument('id', nargs='?')
    instance.add_argument('--json', action='store_true')
    setup = commands.add_parser('setup', help='首次登录后绑定账号；默认从当前微信进程发现数据库目录')
    setup.add_argument('--db-dir', default='auto')
    setup.add_argument('--chat', help='可选：授权目标 wxid / 群 ID 并创建停用的实例')
    setup.add_argument('--instance', default='default')
    commands.add_parser('doctor', help='只读检查安装、依赖、微信版本与运行环境')
    commands.add_parser('config', help='显示需要填写的配置文件位置')
    from wxbridge.debug_cli import build_parser
    build_parser(commands.add_parser('debug', help='配置诊断、队列检查和显式前台调试'), command_dest='debug_command')
    return p


def system_service(action, *, capture=False):
    if action != 'status':
        need_root()
    return subprocess.run(['systemctl', action, UNIT, *(['--no-pager'] if action == 'status' else [])],
                          capture_output=capture, text=capture, check=False)


def current_status(paths):
    try:
        value = control_request('status')
    except PermissionError:
        raise ValueError('没有主控权限，请使用 sudo wxbridge status。') from None
    except (OSError, ValueError):
        result = subprocess.run(['systemctl','show',UNIT,'-p','ActiveState','--value'], capture_output=True, text=True)
        return dict(phase='service_unavailable', service=(result.stdout.strip() or 'not_installed'), instances={})
    live = {}
    try:
        live = private_json(paths.runtime/'account-status.json')
    except (OSError, ValueError):
        pass
    bot = value['components'].get('bot', {})
    fresh = (live.get('pid') == bot.get('pid') and bot.get('state') == 'active'
             and 0 <= time.time()-live.get('updated_at', 0) < 10)
    rows = {}
    for identifier, item in live.get('instances', {}).items() if fresh else []:
        rows[identifier] = {k:item.get(k) for k in ('queue_length','cursor','blocked_reason','models',
            'model_channels','image_generation','quiescing','context','context_policy')}
    login = login_snapshot(paths)
    return {**value, 'snapshot_current':fresh, 'instances':rows, 'login':login}


def login_snapshot(paths):
    try:
        pid = int((paths.runtime/'wechat.pid').read_text())
        for filename in ('login-state.json','login-status.json'):
            try:
                raw = private_json(paths.runtime/filename)
                process = raw.get('process') or {}
                if process.get('pid',raw.get('pid')) != pid:
                    continue
                if process.get('start_time'):
                    actual = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
                    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                    if actual != process['start_time'] or boot != process.get('boot_id'):
                        continue
                return dict(phase=raw.get('phase','unknown'),reason=raw.get('reason'),pid=pid)
            except (OSError,ValueError,KeyError,IndexError):
                continue
    except (OSError,ValueError):
        pass
    return dict(phase='unknown')


def print_status(value):
    print('主机：'+str(value.get('phase','unknown')))
    if 'bot_enabled' in value:
        print('机器人：'+('已启用' if value['bot_enabled'] else '已暂停'))
    if value.get('login',{}).get('phase'):
        print('登录：'+value['login']['phase'])
    for name, item in value.get('components', {}).items():
        print(f"  {name}: {item.get('state','unknown')}  PID {item.get('pid',0)}")
    for identifier, item in value.get('instances', {}).items():
        print(f"实例 {identifier}: 队列 {item.get('queue_length',0)}，游标 {item.get('cursor','—')}")
        if item.get('blocked_reason'):
            print('  发送阻塞：'+item['blocked_reason'])
        for role, context in item.get('context', {}).items():
            if context.get('error'):
                print(f"  {role} 上下文：{context['error']}")
            else:
                print(f"  {role} 上下文：{context.get('chars',0)} 字符，历史 {context.get('retained',0)}"
                      f"/{context.get('selected',0)} 条，裁剪 {context.get('omitted',0)} 条，"
                      f"扫描 {context.get('scanned',0)} 条"+('（达到扫描上限）' if context.get('scan_limit_hit') else ''))


def validate_start(paths):
    code = ("import json,sys; from wxbridge_web.repository import ConfigRepository,ManagementError; "
            "r=ConfigRepository(sys.argv[1]);\n"
            "try:r.validate_start();print(json.dumps({'ok':True}))\n"
            "except ManagementError as e:print(json.dumps({'error':e.message},ensure_ascii=False));sys.exit(2)")
    result = as_user(paths,[str(paths.app/'.venv/bin/python'),'-c',code,str(paths.app)],capture=True,timeout=15)
    if result.returncode:
        try:
            message = json.loads(result.stdout)['error']
        except (ValueError,KeyError):
            message = '配置尚未就绪，请检查账号绑定、已启用实例及模型设置。'
        raise ValueError(message)


def bot_action(args, paths):
    if args.command == 'start':
        validate_start(paths)
    try:
        before = control_request('status')
    except (OSError, ValueError):
        if args.command != 'start':
            raise ValueError('主机服务未运行。使用 sudo wxbridge service start 启动。') from None
        result = system_service('start')
        if result.returncode:
            return result.returncode
        deadline = time.monotonic()+15
        while True:
            try:
                before = control_request('status')
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise ValueError('主控尚未就绪，请运行 wxbridge doctor 或 wxbridge logs --component service。') from None
                time.sleep(.25)
    action = 'apply' if args.command in ('restart','apply') else args.command
    if action == 'apply' and before['components']['bot']['state'] == 'active':
        validate_start(paths)
    previous_pid = before['components']['bot'].get('pid')
    result = control_request(action)
    print(result['message'])
    if not args.wait or result.get('applied') is False:
        return 0
    deadline = time.monotonic()+args.timeout
    reported = 0
    while time.monotonic() < deadline:
        value = control_request('status')
        bot = value['components']['bot']
        if action == 'stop' and bot['state'] == 'inactive':
            print('机器人已暂停；微信与管理台继续运行。')
            return 0
        if action != 'stop' and bot['state'] == 'active' and (action == 'start' or bot['pid'] != previous_pid):
            print('机器人已运行。')
            return 0
        if time.monotonic()-reported >= 5:
            print('等待：'+value.get('phase','unknown'), flush=True)
            reported = time.monotonic()
        time.sleep(.5)
    raise ValueError('等待超时，操作仍可能在进行；请用 wxbridge status 查看。')


def instance_action(args, paths):
    if args.action != 'list' and not args.id:
        raise ValueError('请指定实例 ID。')
    # Config files remain owned by the service account, even when an administrator runs this CLI.
    code = ("import json,sys; from wxbridge_web.repository import ConfigRepository; "
            "r=ConfigRepository(sys.argv[1]); action=sys.argv[2]; identifier=sys.argv[3]; "
            "result=r.list() if action=='list' else r.request_instance(identifier,action,r.get(identifier)['revision']); "
            "print(json.dumps(result,ensure_ascii=False))")
    result = as_user(paths, [str(paths.app/'.venv/bin/python'),'-c',code,str(paths.app),args.action,args.id or ''],
                     capture=True, timeout=15)
    if result.returncode:
        raise ValueError('实例操作失败，请核对实例 ID、模型配置和发送授权清单。')
    value = json.loads(result.stdout)
    if args.json:
        print(json.dumps(value,ensure_ascii=False,indent=2))
    elif args.action == 'list':
        for item in value:
            print(f"{item['id']}  {'启用' if item['enabled'] else '停用'}  {item['chat']}  {item.get('reply_model','')}")
    else:
        print('实例操作已提交；主机机器人运行时自动处理，暂停时等待下次启动。')
    return 0


def main(argv=None, *, root=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    args = parser().parse_args(argv)
    paths = Paths(app=(args.root or Path(root or '/opt/wxbridge')).resolve())
    try:
        if args.command == 'debug':
            from wxbridge.debug_cli import execute
            args.command = args.debug_command
            args.config = args.config or str(paths.app/'config.json')
            return execute(args)
        if args.command == 'status':
            value = current_status(paths)
            print(json.dumps(value,ensure_ascii=False,indent=2)) if args.json else print_status(value)
            return 0
        if args.command in ('start','stop','restart','apply'):
            return bot_action(args, paths)
        if args.command == 'service':
            return system_service(args.action).returncode
        if args.command == 'login':
            if args.vnc:
                if args.status:
                    raise ValueError('--vnc 与 --status 不能同时使用。')
                return as_user(paths,['/usr/bin/bash',str(paths.app/'scripts/login-vnc.sh')]).returncode
            cmd = [str(paths.app/'.venv/bin/python'),str(paths.app/'scripts/wechat-login.py')]
            if args.status:
                cmd.append('--status')
            return as_user(paths,cmd).returncode
        if args.command == 'logs':
            if args.component == 'service':
                cmd = ['journalctl','-u',UNIT,'-n',str(args.lines),'--no-pager']
                if args.follow:cmd.append('-f')
            else:
                name = {'bot':'bot-live.log','wechat':'session-supervisor.log','web':'web.log','sender':'sender.log'}[args.component]
                path = paths.runtime/name
                if not path.is_file() or path.is_symlink():
                    raise ValueError('运行日志尚未生成。')
                cmd = ['tail','-n',str(args.lines),*(['-F'] if args.follow else []),str(path)]
            return subprocess.run(cmd).returncode
        if args.command == 'instance':
            return instance_action(args, paths)
        if args.command == 'setup':
            from .setup import bind_account
            print(json.dumps(bind_account(paths,args.db_dir,chat=args.chat,identifier=args.instance),ensure_ascii=False,indent=2))
            return 0
        if args.command == 'doctor':
            from .install import doctor
            result = doctor(paths)
            for item in result:
                print(('OK   ' if item['ok'] else 'FAIL ')+item['name']+' '+item.get('detail',''))
            return int(any(not item['ok'] for item in result))
        if args.command == 'config':
            for path in (paths.app/'config.json',paths.app/'src/provider/.env',paths.app/'instance',paths.settings):
                print(path)
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print('wxbridge: '+message, file=sys.stderr)
        return 1
    return 0
