"""Host-only Compose dispatch; credentials never appear in command arguments."""
import json
import os
from pathlib import Path
import subprocess
import secrets
import hashlib


from wxbridge.host.bootstrap import initialize_data


def initialize(directory, image, port):
    if os.name == 'posix' and os.geteuid() != 0:
        raise ValueError('容器项目初始化需要 sudo，以设置账号目录和密钥权限。')
    if not 1 <= port <= 65535 or not image or any(c.isspace() for c in image):
        raise ValueError('invalid_container_project')
    if directory.exists() and any(directory.iterdir()):
        spec = read_project(directory)
        service = spec['services']['account']
        network = service.get('network_mode')
        same_port = (str(service.get('environment',{}).get('WXBRIDGE_WEB_PORT')) == str(port)
                     if network == 'host' else service.get('ports') == [f'127.0.0.1:{port}:8730'])
        if service.get('image') != image or not same_port:
            raise ValueError('existing_container_project_differs')
        for name in ('config','home','settings','service','catalog'):
            path = directory/name
            if not path.is_dir() or path.is_symlink():
                raise ValueError('invalid_existing_container_directory')
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('config', 'home', 'settings', 'service', 'catalog'):
        (directory/name).mkdir(mode=0o700)
    password = directory/'settings/web-password'
    password.write_text(secrets.token_urlsafe(32)+'\n',encoding='utf-8')
    password.chmod(0o600)
    provider = directory/'provider.env'
    provider.write_text('# Fill in the keys for the providers you use. Direct connections are the default.\n'
                        'OPENAI_API_KEY=\nANTHROPIC_API_KEY=\nDEEPSEEK_API_KEY=\nGEMINI_API_KEY=\n'
                        '# Optional: <PROVIDER>_BASE_URL for a gateway; <PROVIDER>_PROXY_URL for a proxy.\n',
                        encoding='utf-8')
    provider.chmod(0o440 if os.name == 'posix' else 0o600)
    directory.chmod(0o750)
    if hasattr(os, 'chown'):
        os.chown(directory,0,10001)
        os.chown(provider,0,10001)
    service = dict(image=image, init=True, restart='unless-stopped', stop_grace_period='10m',
                   cap_add=['SYS_PTRACE'], security_opt=['no-new-privileges:true'],
                   environment=dict(WXBRIDGE_RUNTIME='container',WXBRIDGE_NETWORK='host',WXBRIDGE_WEB_PORT=str(port)),
                   network_mode='host',
                   tmpfs=['/run/wxbridge-trial:size=512m,mode=0700,uid=10001,gid=10001',
                          '/run/wxbridge-control:mode=0750',
                          '/run/wechat-linux-images:size=16m,mode=0700', '/tmp:size=256m,mode=1777'],
                   ulimits=dict(core=dict(soft=0,hard=0)),
                   shm_size='256m', mem_limit='4g', memswap_limit='4g', pids_limit=512,
                   volumes=[dict(type='bind', source='./'+source, target=target,
                                 read_only=source == '.', bind=dict(create_host_path=False))
                            for source, target in (
                                ('config','/var/lib/wxbridge-config'),('home','/var/lib/wxbridge'),
                                ('settings','/etc/wxbridge'),('service','/var/lib/wxbridge-service'),
                                ('catalog','/var/lib/wxbridge-web'),('.','/run/secrets/wxbridge'))]
                           + [dict(type='volume',source='sensitive',target='/run/wxbridge-sensitive',
                                   volume=dict(nocopy=True))],
                   logging=dict(driver='json-file',options={'max-size':'10m','max-file':'3'}),
                   healthcheck=dict(test=['CMD','/opt/wxbridge/.venv/bin/python','-m',
                                          'wxbridge.host.container','health'],interval='30s',timeout='5s',retries=3))
    document = dict(name='wxbridge-'+hashlib.sha256(str(directory).encode()).hexdigest()[:12],services=dict(account=service),volumes=dict(sensitive=dict(driver='local',
                    driver_opts=dict(type='tmpfs',device='tmpfs',o='size=1g,mode=0700,uid=10001,gid=10001,noswap'))))
    (directory/'compose.json').write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8')


def read_project(directory):
    spec = json.loads((directory/'compose.json').read_text(encoding='utf-8'))
    service = spec['services']['account']
    if (service.get('privileged') or service.get('pid') or service.get('ipc') == 'host'
            or not service.get('init')):
        raise ValueError('container_requires_isolated_namespaces_and_init')
    if service.get('network_mode')=='host' and (service.get('ports') or service.get('extra_hosts')):
        raise ValueError('host_network_requires_no_port_or_host_mapping')
    return spec


def execute(args, argv):
    try:
        if not args.project_dir:
            raise ValueError('--project-dir is required')
        directory = args.project_dir.resolve()
        if args.command == 'container-init':
            initialize(directory,args.image,args.port)
            return 0
        if os.environ.get('WXBRIDGE_RUNTIME') == 'container':
            raise ValueError('compose_dispatch_inside_container_forbidden')
        read_project(directory)
        base = ['docker','compose','--project-directory',str(directory),'-f',str(directory/'compose.json')]
        command = args.command
        if command in ('start','stop','restart'):
            tail = {'start':['up','-d'],'stop':['stop'],'restart':['restart']}[command]
        elif command == 'logs' and args.component == 'service':
            tail = ['logs','--tail',str(args.lines),*(['-f'] if args.follow else []),'account']
        elif command in ('enable','disable'):
            raise ValueError('容器自启动策略请修改 compose.json 中的 restart。')
        elif command == 'status':
            probe = subprocess.run(base+['ps','--all','--format','json'],capture_output=True,text=True,check=False)
            try:
                containers = json.loads(probe.stdout or '[]')
                if isinstance(containers, dict):containers = [containers]
            except json.JSONDecodeError:
                containers = [json.loads(line) for line in probe.stdout.splitlines() if line.strip()]
            running = any(row.get('State') == 'running' for row in containers)
            if args.json:
                application = None
                code = probe.returncode
                if running and not code:
                    live = subprocess.run(base+['exec','-T','account','wxbridge','status','--json'],
                                          capture_output=True,text=True,check=False)
                    code = live.returncode
                    if not code:application = json.loads(live.stdout)
                print(json.dumps(dict(containers=containers,application=application),ensure_ascii=False))
                return code
            print(probe.stdout,end='')
            if probe.returncode or not running:
                return probe.returncode
            tail = ['exec','-T','account','wxbridge','status']
        else:
            forwarded = list(argv)
            for flag in ('--backend','--project-dir'):
                if flag in forwarded:
                    index = forwarded.index(flag)
                    del forwarded[index:index+2]
                else:
                    forwarded = [v for v in forwarded if not v.startswith(flag+'=')]
            tail = ['exec',*(['-T'] if not os.isatty(0) else []),'account','wxbridge',*forwarded]
        return subprocess.run(base+tail,check=False).returncode
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print('wxbridge: '+(str(exc) if isinstance(exc,ValueError) else type(exc).__name__))
        return 1
