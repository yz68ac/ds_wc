"""Local/SSH-forwarded console. Real lifecycle control is opt-in and uses OS authorization."""
import argparse
import getpass
import ipaddress
import os
import sys
from pathlib import Path
import tempfile


def bind_host(value):
    if value == 'localhost':
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError('监听地址必须是本机地址或私有 IPv4 地址。') from None
    private_v4 = address.version == 4 and any(address in ipaddress.ip_network(net) for net in (
        '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not address.is_loopback and not private_v4:
        raise argparse.ArgumentTypeError('只允许本机地址或私有 IPv4 地址，不能监听所有网卡。')
    return str(address)


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='WXBridge Web 管理台（默认仅监听本机）')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2],help='包含 config.json 和 instance/ 的项目目录')
    parser.add_argument('--host',type=bind_host,default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8730)
    parser.add_argument('--password-file',type=Path,help='单独存放管理口令的 UTF-8 私有文件；不会读取 provider 密钥')
    parser.add_argument('--allow-service-control',action='store_true',help='开放本机主控的机器人启停接口')
    parser.add_argument('--control-socket',default='/run/wxbridge-control/control.sock',help='本机主控 Unix socket')
    parser.add_argument('--demo',action='store_true',help='使用临时合成实例预览，不连接真实微信')
    parser.add_argument('--catalog-file',type=Path,help='模型列表缓存文件；默认使用 provider 配置旁的 models.json')
    args=parser.parse_args()
    temporary=None
    try:
        from wxbridge_web.app import create_app
        from wxbridge_web.services import ServiceManager
        import uvicorn
        if args.demo:
            from wxbridge_web.demo import DEMO_PASSWORD, DemoServices, seed
            temporary=tempfile.TemporaryDirectory(prefix='wxbridge-web-demo-')
            root=Path(temporary.name)
            seed(root)
            password=DEMO_PASSWORD
            services=DemoServices(root)
            print('演示模式：只操作合成数据，口令为 wxbridge-demo-local。',flush=True)
        else:
            root=args.root.resolve()
            if not (root/'config.json').is_file():
                parser.error('项目目录缺少 config.json。')
            if args.password_file:
                if args.password_file.is_symlink():
                    parser.error('管理口令文件不能是链接。')
                path=args.password_file.resolve()
                if not path.is_file():
                    parser.error('管理口令文件不存在或是链接。')
                if os.name=='posix' and path.stat().st_mode & 0o077:
                    parser.error('管理口令文件必须限制为仅当前用户可读，例如 chmod 600。')
                password=path.read_text(encoding='utf-8-sig').strip()
            else:
                password=os.environ.get('WXBRIDGE_WEB_PASSWORD') or getpass.getpass('管理口令（至少12个字符）：')
            services=ServiceManager(allow_control=args.allow_service_control,control_socket=args.control_socket)
        allowed_hosts=['localhost','127.0.0.1','[::1]',args.host]
        app=create_app(root,password,services=services,demo=args.demo,catalog_file=args.catalog_file,allowed_hosts=allowed_hosts)
        address='['+args.host+']' if ':' in args.host else args.host
        print(f'Web 管理台：http://{address}:{args.port} （不会自动启动机器人）',flush=True)
        uvicorn.run(app,host=args.host,port=args.port,access_log=False,proxy_headers=False)
    finally:
        if temporary:
            temporary.cleanup()


if __name__=='__main__':
    main()
