"""Fresh-host Debian/Ubuntu bootstrap. Existing installations are never overwritten."""
import argparse
from dataclasses import dataclass
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

from .host import Paths, UNIT, USER, atomic_file, atomic_json, service_account, need_root

MOUNT_UNIT = r'run-wxbridge\x2dtrial.mount'
PACKAGES = ('python3-venv','python3-gi','python3-pycryptodome','python3-zstandard',
            'gir1.2-atspi-2.0','gir1.2-gtk-3.0','at-spi2-core','xvfb','xauth',
            'dbus-x11','fluxbox','xdotool','libde265-0','ffmpeg','ca-certificates')
FRIDA_URL = ('https://files.pythonhosted.org/packages/52/0d/68f5453d9baa0ae5e0038e496bded5ec1c35fd5972bb59a9f98b1c12ff59/'
             'frida-17.24.3-cp37-abi3-manylinux_2_5_x86_64.whl')
ROOT_FILES = frozenset(('.env.example','.gitignore','pyproject.toml','uv.lock','requirements.txt',
    'requirements-test.txt','config.defaults.json','MANIFEST.in','README.md','release-manifest.json'))
ROOT_DIRS = frozenset(('src','scripts','examples','prompts','resources','tests','docs'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def public_files(source):
    rules = runpy.run_path(str(source/'scripts/check-repository.py'))
    declared = None
    manifest = source/'release-manifest.json'
    if manifest.exists():
        metadata = json.loads(manifest.read_text())
        if not isinstance(metadata,dict) or not isinstance(metadata.get('files'),dict):
            raise ValueError('发布清单格式无效。')
        declared = set(metadata['files']) | {'release-manifest.json'}
        for name in declared:
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or ':' in name
                    or any(p in ('','.','..') for p in name.split('/'))):
                raise ValueError('发布清单路径无效。')
    result = []
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        parts = relative.parts
        if (declared is not None and relative.as_posix() not in declared
                or declared is None and (not parts or parts[0] not in ROOT_DIRS and str(relative) not in ROOT_FILES)):
            continue
        if any(p in ('.git','.venv','venv','node_modules','__pycache__','.test-tmp','binary','installer','build')
               or p.endswith('.egg-info') for p in parts):
            continue
        if 'dist' in parts and parts[:3] != ('src','webui','dist'):
            continue
        if path.is_symlink():
            raise ValueError('发布源码包含符号链接。')
        if not path.is_file() or path.suffix in ('.pyc','.pyo','.tsbuildinfo'):
            continue
        name = relative.as_posix()
        if name == 'src/provider/models.json':
            continue
        if rules['private_path'](name):
            continue
        if path.stat().st_size > 32*1024*1024 or rules['credential_lines'](path.read_bytes()):
            raise ValueError('发布源码包含过大文件或疑似凭据。')
        result.append(relative)
    return sorted(result)


def existing_targets(paths):
    return [p for p in (paths.app, paths.helper, paths.settings, Path('/usr/local/bin/wxbridge'),
        Path('/usr/local/lib/wxbridge-recovery'), Path('/etc/systemd/system')/UNIT,
        Path('/etc/systemd/system')/MOUNT_UNIT, Path('/etc/wxbridge'),
        Path('/var/lib/wxbridge-service'),Path('/var/lib/wxbridge-web')) if p.exists() or p.is_symlink()]


def verify_source(source, files):
    manifest = source/'release-manifest.json'
    if not manifest.exists():
        return False
    value = json.loads(manifest.read_text())
    expected = value.get('files', {})
    if value.get('format') != 1 or set(expected) != {p.as_posix() for p in files if p.as_posix() != 'release-manifest.json'}:
        raise ValueError('发布清单与实际安装文件不一致。')
    for name, item in expected.items():
        if sha(source/name) != item.get('sha256'):
            raise ValueError('发布代码校验失败。')
    return True


def validate_host(web_host):
    from wxbridge_web.__main__ import bind_host
    # bind_host itself uses only the standard library and does not start FastAPI.
    try:
        bind_host(web_host)
    except argparse.ArgumentTypeError:
        raise ValueError('管理台地址必须是本机地址或私有 IPv4。') from None
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64','amd64'):
        raise ValueError('安装器要求 Linux x86_64。')
    if sys.version_info < (3,11):
        raise ValueError('系统 Python 需要 3.11 或以上。')
    if not Path('/run/systemd/system').is_dir() or not shutil.which('apt-get'):
        raise ValueError('当前安装器支持使用 systemd 和 apt 的 Debian/Ubuntu 主机。')
    try:
        with socket.socket(socket.AF_INET6 if ':' in web_host else socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.bind((web_host,8730))
    except OSError:
        raise ValueError('管理台地址不属于本机，或 8730 端口已被使用。') from None


def verify_media(source, deb=None, wheel=None):
    profile = json.loads((source/'src/wechat_linux/native/profile.json').read_text())
    manifest = json.loads((source/'resources/wechat/linux-x86_64/4.1.13.26/manifest.json').read_text())
    executable = Path('/opt/wechat/wechat')
    if executable.exists():
        if sha(executable) != profile['sha256']:
            raise ValueError('已有微信程序与当前原生接口不匹配；安装器不会覆盖。')
    elif deb is None:
        raise ValueError('请通过 --wechat-deb 提供匹配项目版本的微信安装包。')
    if deb and sha(deb) != manifest['artifacts']['installer']['sha256']:
        raise ValueError('微信安装包 SHA-256 与项目记录不匹配。')
    native = runpy.run_path(str(source/'scripts/install-native-helper.py'))
    if wheel and sha(wheel) != native['WHEEL_SHA256']:
        raise ValueError('Frida wheel 校验失败。')
    return profile, native['WHEEL_SHA256']


def check_kernel(run=subprocess.run):
    with tempfile.TemporaryDirectory(prefix='.wxbridge-noswap-', dir='/run') as directory:
        mounted = False
        try:
            run(['mount','-t','tmpfs','-o','size=1M,mode=0700,noswap','tmpfs',directory], check=True)
            mounted = True
        finally:
            if mounted:
                run(['umount',directory], check=True)


def validate_password(value):
    if not isinstance(value,str) or not 12 <= len(value) <= 1024 or '\n' in value or '\r' in value:
        raise ValueError('管理台密码需要12至1024个字符，且不能包含换行。')
    return value


def password_input(path=None):
    if path:
        import stat
        flags=os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0)
        with os.fdopen(os.open(path,flags),'rb') as stream:
            info=os.fstat(stream.fileno())
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError('密码输入文件必须是私有普通文件（0600）。')
            return validate_password(stream.read(4097).decode('utf-8-sig').strip())
    if not sys.stdin.isatty():
        raise ValueError('非交互安装请使用 --web-password-file；不要把密码放入命令参数。')
    first = getpass.getpass('设置管理台密码：')
    if first != getpass.getpass('再次输入：'):
        raise ValueError('两次密码不同。')
    return validate_password(first)


def render_units(source, account, web_host):
    mount = (source/'scripts/systemd/runtime.mount.in').read_text()
    mount = mount.replace('@UID@',str(account.pw_uid)).replace('@GID@',str(account.pw_gid))
    service = (source/'scripts/systemd/wxbridge.service').read_text()
    if web_host != '127.0.0.1':
        service = service.replace('-m wxbridge_service --root /opt/wxbridge\n',
                                  '-m wxbridge_service --root /opt/wxbridge --web-host '+web_host+'\n')
    return mount, service


def initial_config(paths):
    return dict(db_dir='', provider_env=str(paths.app/'src/provider/.env'),
        keys_file=str(paths.runtime/'keys.json'), decrypted_dir=str(paths.runtime/'decrypted'),
        state_file=str(paths.runtime/'state.json'), log_file=str(paths.runtime/'bot.log'),
        queue=dict(file=str(paths.runtime/'queue.sqlite3')), memory=dict(directory=str(paths.home/'memory')),
        ui=dict(backend='native_service', dry_run=False),
        native_api=dict(enabled=True, socket=str(paths.runtime/'native-api.sock'),
            helper_socket=str(paths.runtime/'native-helper.sock'), allowed_chat_ids=['filehelper'],
            max_text_bytes=4096, max_pending=16, max_requests=1024))


def credential_free_template(raw):
    lines = []
    for line in raw.decode('utf-8-sig').splitlines():
        key, sep, _ = line.partition('=')
        if sep and not key.lstrip().startswith('#') and any(part in key.upper() for part in ('API_KEY','TOKEN','SECRET','PASSWORD')):
            line = key+'='
        lines.append(line)
    return ('\n'.join(lines)+'\n').encode()


def fetch_frida(destination, expected, *, proxy=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'http':proxy,'https':proxy} if proxy else {}))
    # Create exclusively; an interrupted download never becomes installation input.
    with destination.open('xb') as stream, opener.open(FRIDA_URL, timeout=30) as response:
        total = 0
        while chunk := response.read(1024*1024):
            total += len(chunk)
            if total > 32*1024*1024:
                raise ValueError('Frida wheel 超过大小上限。')
            stream.write(chunk)
    if sha(destination) != expected:
        raise ValueError('下载的 Frida wheel 校验失败。')
    return destination


def dependency_health():
    from packaging.requirements import Requirement
    roots = ['wxbridge','model-provider','wechat-linux-adapter','openai','anthropic','google-genai','Pillow','fastapi','uvicorn','pycryptodome','zstandard','zxing-cpp','qrcode']
    seen, pending, failures = set(), [(n,frozenset()) for n in roots], []
    while pending:
        name, extras = pending.pop()
        if (name.lower(),extras) in seen:
            continue
        seen.add((name.lower(),extras))
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            failures.append(name+': missing')
            continue
        for text in importlib.metadata.requires(name) or []:
            r = Requirement(text)
            if r.marker and not any(r.marker.evaluate({'extra':e}) for e in (extras or {''})):
                continue
            try:
                found = importlib.metadata.version(r.name)
            except importlib.metadata.PackageNotFoundError:
                failures.append(r.name+': missing')
                continue
            if r.specifier and not r.specifier.contains(found, prereleases=True):
                failures.append(r.name+': incompatible')
            pending.append((r.name,frozenset(r.extras)))
    return failures


def doctor(paths):
    result = []
    try:
        cfg = json.loads((paths.app/'config.json').read_text())
    except (OSError,ValueError):
        cfg = {}
    def add(name, ok, detail=''):
        result.append(dict(name=name,ok=bool(ok),detail=detail))
    add('Linux x86_64',sys.platform == 'linux' and platform.machine().lower() in ('x86_64','amd64'))
    try:
        failures = dependency_health()
        add('运行依赖',not failures, ', '.join(failures))
    except ImportError:
        add('运行依赖',False,'请在项目虚拟环境运行。')
    for name in ('Xvfb','dbus-launch','xdotool','xauth','fluxbox'):
        add(name,shutil.which(name) is not None)
    if cfg.get('ui',{}).get('backend') != 'native_service':
        add('xclip（桌面发送）',shutil.which('xclip') is not None)
    gui = subprocess.run(['/usr/bin/python3','-c',
        "import gi; gi.require_version('Atspi','2.0'); gi.require_version('Gdk','3.0'); from gi.repository import Atspi,Gdk"],
        capture_output=True,timeout=10)
    add('系统 GI / AT-SPI / Gdk',gui.returncode == 0)
    try:
        profile = json.loads((paths.app/'src/wechat_linux/native/profile.json').read_text())
        add('微信版本校验',sha('/opt/wechat/wechat') == profile['sha256'])
    except OSError:
        add('微信版本校验',False)
    try:
        from wxbridge.storage.memoryfs import is_noswap_tmpfs
        add('私有内存挂载',is_noswap_tmpfs(paths.runtime.resolve(),Path('/proc/self/mountinfo').read_text()))
    except OSError:
        add('私有内存挂载',False)
    try:
        account = service_account()
        info = paths.runtime.stat()
        add('运行目录权限',info.st_uid == account.pw_uid and not info.st_mode & 0o077)
    except (OSError,KeyError):
        add('运行目录权限',False)
    try:
        cfg = json.loads((paths.app/'config.json').read_text())
        add('账号绑定',bool(cfg.get('db_dir')), '首次扫码后运行 sudo wxbridge setup。' if not cfg.get('db_dir') else '')
        from wxbridge.config import load_config
        load_config(paths.app/'config.json')
        add('实例配置',True)
    except (OSError,ValueError,KeyError):
        add('实例配置',False)
    return result


def install(source, args, *, run=subprocess.run):
    paths = Paths()
    validate_host(args.web_host)
    conflicts = existing_targets(paths)
    if conflicts:
        raise ValueError('检测到已有安装或配置，拒绝覆盖：'+', '.join(map(str,conflicts)))
    profile, frida_hash = verify_media(source,args.wechat_deb,args.frida_wheel)
    files = public_files(source)
    verified_source = verify_source(source,files)
    frontend = source/'src/webui/dist/index.html'
    if not frontend.is_file():
        raise ValueError('发布包缺少 src/webui/dist；先 npm ci && npm run build，或使用含前端的发布包。')
    if not (source/'requirements.txt').is_file():
        raise ValueError('发布包缺少锁定的 requirements.txt；先运行 python scripts/dependencies.py。')
    plan = dict(target=str(paths.app), user=USER, web_host=args.web_host,
        packages=list(PACKAGES)+(['x11vnc'] if args.with_vnc else []), public_files=len(files),
        credentials_copied=False, initial_bot_enabled=False, media_hashes_checked=True,
        release_manifest_checked=verified_source,
        existing_wechat=Path('/opt/wechat/wechat').exists(), download_frida=args.frida_wheel is None)
    print(json.dumps(plan,ensure_ascii=False,indent=2),flush=True)
    if args.dry_run:
        return plan
    need_root()
    password = password_input(args.web_password_file)
    check_kernel(run)
    # From this point, failures leave explicitly named installation files for inspection.
    if not args.skip_system_deps:
        run(['apt-get','update'],check=True)
        run(['apt-get','install','-y',*plan['packages']],check=True)
    if not Path('/opt/wechat/wechat').exists():
        run(['apt-get','install','-y',str(args.wechat_deb.resolve())],check=True)
    if sha('/opt/wechat/wechat') != profile['sha256']:
        raise ValueError('安装后的微信程序与当前接口不匹配。')
    try:
        account = service_account()
        if account.pw_uid == 0 or Path(account.pw_dir) != paths.home:
            raise ValueError('已有 wxbridge 用户的身份或 HOME 不匹配。')
    except KeyError:
        run(['useradd','--system','--user-group','--create-home','--home-dir',str(paths.home),
             '--shell','/usr/sbin/nologin',USER],check=True)
        account = service_account()
    if paths.home.is_symlink() or paths.home.stat().st_uid != account.pw_uid:
        raise ValueError('服务用户 HOME 的路径或属主不正确。')
    paths.home.chmod(0o700)
    paths.app.mkdir(mode=0o755)
    inventory = {}
    for relative in files:
        target = paths.app/relative
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        atomic_file(target,(source/relative).read_bytes(),mode=0o755 if target.suffix == '.sh' else 0o644)
        inventory[relative.as_posix()] = sha(target)
    for directory in (paths.app/'instance',paths.home/'memory'):
        directory.mkdir(mode=0o700,exist_ok=True)
        os.chown(directory,account.pw_uid,account.pw_gid)
        directory.chmod(0o700)
    run(['/usr/bin/python3','-m','venv','--system-site-packages',str(paths.app/'.venv')],check=True)
    python = paths.app/'.venv/bin/python'
    pip = [str(python),'-m','pip','--isolated','install','--index-url','https://pypi.org/simple']
    if args.package_proxy:
        pip += ['--proxy',args.package_proxy]
    run([*pip,'-r',str(paths.app/'requirements.txt'),'-e',str(paths.app)+'[database,login]',
         '-e',str(paths.app/'src/provider'),'-e',str(paths.app/'src/wechat_linux')],check=True)
    scanner = Path('/usr/local/lib/wxbridge-recovery')
    scanner.mkdir(parents=True,mode=0o755)
    for name in ('wxbridge','wechat_linux'):
        for path in (paths.app/'src'/name).rglob('*'):
            if path.is_file() and path.suffix in ('.py','.json','.js','.c') and '__pycache__' not in path.parts:
                target = scanner/name/path.relative_to(paths.app/'src'/name)
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                atomic_file(target,path.read_bytes(),mode=0o644)
    atomic_json(paths.app/'config.json',initial_config(paths),uid=account.pw_uid,gid=account.pw_gid)
    atomic_json(paths.settings,{'db_dir':''})
    atomic_file(paths.app/'src/provider/.env',credential_free_template((source/'.env.example').read_bytes()),uid=account.pw_uid,gid=account.pw_gid)
    web_settings = Path('/etc/wxbridge')
    web_settings.mkdir(mode=0o750)
    os.chown(web_settings,0,account.pw_gid)
    web_settings.chmod(0o750)
    atomic_file('/etc/wxbridge/web-password',(password+'\n').encode(),uid=account.pw_uid,gid=account.pw_gid)
    with tempfile.TemporaryDirectory(prefix='wxbridge-media-') as media:
        wheel = args.frida_wheel or fetch_frida(Path(media)/'frida.whl',frida_hash,proxy=args.package_proxy)
        run([str(python),str(paths.app/'scripts/install-native-helper.py'),'--wheel',str(wheel),
             '--allow','filehelper'],check=True)
    mount, service = render_units(source,account,args.web_host)
    atomic_file(Path('/etc/systemd/system')/MOUNT_UNIT,mount.encode(),mode=0o644)
    atomic_file(Path('/etc/systemd/system')/UNIT,service.encode(),mode=0o644)
    launcher = '#!/bin/sh\nexec /opt/wxbridge/.venv/bin/python -m wxbridge "$@"\n'
    atomic_file('/usr/local/bin/wxbridge',launcher.encode(),mode=0o755)
    state = Path('/var/lib/wxbridge-service');state.mkdir(mode=0o700);state.chmod(0o700)
    atomic_json(state/'state.json',dict(schema=1,bot_enabled=False))
    atomic_json(state/'installation.json',dict(schema=1,files=inventory,uid=account.pw_uid,gid=account.pw_gid,
                                               web_host=args.web_host,wechat_sha256=profile['sha256']))
    run(['systemctl','daemon-reload'],check=True)
    run(['systemctl','enable','--now',UNIT],check=True)
    print('安装完成。机器人保持暂停；请运行 sudo wxbridge login，扫码后 sudo wxbridge setup。',flush=True)
    if args.web_host != '127.0.0.1':
        print('管理台监听 '+args.web_host+':8730；请在主机防火墙按实际局域网网段放行。')
    return plan


def main(argv=None, *, source):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wechat-deb',type=Path,help='项目固定兼容版本的本地微信安装包')
    p.add_argument('--frida-wheel',type=Path,help='可选：固定版本 wheel；省略时从官方 PyPI 文件源下载并校验')
    p.add_argument('--web-host',default='127.0.0.1',help='默认仅本机；局域网访问填写本机私有 IPv4')
    p.add_argument('--web-password-file',type=Path,help='非交互安装读取0600密码文件；不接受明文密码参数')
    p.add_argument('--package-proxy',help='仅安装依赖时使用的 HTTP 代理，不写入 provider 配置')
    p.add_argument('--with-vnc',action='store_true')
    p.add_argument('--skip-system-deps',action='store_true')
    p.add_argument('--dry-run',action='store_true',help='只读核对和显示安装计划，不安装或启动任何程序')
    args = p.parse_args(argv)
    try:
        if args.package_proxy:
            from urllib.parse import urlsplit
            v = urlsplit(args.package_proxy)
            if (v.scheme != 'http' or not v.hostname or v.username or v.password or v.query or v.fragment
                    or v.path not in ('','/') or any(c.isspace() for c in args.package_proxy)):
                raise ValueError('安装代理需要不含凭据的 HTTP 地址。')
        install(Path(source).resolve(),args)
        return 0
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('安装未完成：'+(str(exc) if isinstance(exc,ValueError) else type(exc).__name__),file=sys.stderr)
        return 1
