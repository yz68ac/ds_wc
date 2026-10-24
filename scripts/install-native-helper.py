"""One-time Linux installation of the isolated helper; never overwrites existing tools.

Run as root after reviewing a committed release. The official Frida wheel is an
explicit local input, verified before installation; no downloads or system pip.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import venv
import zipfile

FRIDA_VERSION='17.24.3'
WHEEL_SHA256='2ba706b7385d56111af5c6cced55361168d3d93babdb300d2ea2b7f76d58a289'
TARGET=Path('/opt/wxbridge-native-helper')
UNIT=Path('/etc/systemd/system/wxbridge-native-helper.service')
SHORTCUT=Path('/usr/local/bin/ds-native')


def install(wheel,user,allowed):
    import grp
    import pwd
    if os.geteuid()!=0 or sys.platform!='linux':raise ValueError('linux_root_required')
    account=pwd.getpwnam(user)
    if account.pw_uid==0:raise ValueError('nonroot_client_required')
    for target in (TARGET,UNIT,SHORTCUT):
        if target.exists() or target.is_symlink():raise ValueError('installation_target_exists')
    with wheel.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=WHEEL_SHA256:
            raise ValueError('frida_wheel_hash_mismatch')
    source=Path(__file__).resolve().parents[1]/'wxbridge'/'native'
    # Validate input before creating any installation target.
    sys.path.insert(0,str(source))
    import helper
    for chat in allowed:
        if not helper.CID.fullmatch(chat):raise ValueError('invalid_allowed_chat')
    with zipfile.ZipFile(wheel) as archive:
        prefix=f'frida-{FRIDA_VERSION}.dist-info/'
        for name in archive.namelist():
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or not name.startswith(('frida/',prefix)):
                raise ValueError('invalid_wheel_inventory')
        for name,digest,size in csv.reader(io.StringIO(archive.read(prefix+'RECORD').decode())):
            if digest:
                raw=archive.read(name)
                actual=base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
                if digest!='sha256='+actual or len(raw)!=int(size):raise ValueError('wheel_record_mismatch')
        TARGET.mkdir(mode=0o755)
        environment=TARGET/'venv'
        venv.EnvBuilder(with_pip=False,symlinks=True).create(environment)
        python=environment/'bin/python'
        site=Path(subprocess.check_output([str(python),'-I','-c',
                    'import sysconfig;print(sysconfig.get_path("purelib"))'],text=True).strip())
        if not site.is_relative_to(environment):raise ValueError('invalid_venv_path')
        archive.extractall(site)
    for name in ('helper.py','agent.js','profile.json'):
        shutil.copyfile(source/name,TARGET/name);(TARGET/name).chmod(0o644)
    config=dict(socket='/run/wxbridge-trial/native-helper.sock',uid=account.pw_uid,
                allowed_chat_ids=list(dict.fromkeys(allowed)),assets=str(TARGET),worker_timeout_seconds=15)
    config_file=TARGET/'helper-config.json'
    config_file.write_text(json.dumps(config,indent=2));config_file.chmod(0o600)
    # Verify a new isolated interpreter, rather than the installer's import environment.
    version=subprocess.check_output([str(python),'-I','-c','import frida;print(frida.__version__)'],text=True).strip()
    if version!=FRIDA_VERSION:raise ValueError('frida_import_mismatch')
    helper.configuration(config_file)
    UNIT.write_text(f'''[Unit]
Description=Private native WeChat text submission helper
After=wxbridge-session.service
Requires=wxbridge-session.service
PartOf=wxbridge-session.service

[Service]
Type=simple
User=root
Group={grp.getgrgid(account.pw_gid).gr_name}
WorkingDirectory={TARGET}
ExecStart={python} -I {TARGET}/helper.py --serve {config_file}
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/run/wxbridge-trial
TimeoutStopSec=8
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
''')
    UNIT.chmod(0o644)
    SHORTCUT.parent.mkdir(parents=True,exist_ok=True)
    SHORTCUT.write_text(f'''#!/bin/sh
set -eu
cd /opt/wxbridge
if [ "$(id -u)" -eq 0 ]; then
    exec /usr/sbin/runuser -u {account.pw_name} -- /usr/bin/python3 -m wxbridge native-api "$@"
fi
exec /usr/bin/python3 -m wxbridge native-api "$@"
''')
    SHORTCUT.chmod(0o755)
    provenance=dict(frida_version=version,wheel_sha256=WHEEL_SHA256,uid=account.pw_uid,gid=account.pw_gid,
        source='https://files.pythonhosted.org/packages/52/0d/68f5453d9baa0ae5e0038e496bded5ec1c35fd5972bb59a9f98b1c12ff59/frida-17.24.3-cp37-abi3-manylinux_2_5_x86_64.whl',
        files={name:hashlib.sha256((TARGET/name).read_bytes()).hexdigest() for name in ('helper.py','agent.js','profile.json')})
    (TARGET/'installation.json').write_text(json.dumps(provenance,indent=2))
    (TARGET/'installation.json').chmod(0o600)
    print(json.dumps(dict(installed=True,service_started=False,frida=version)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',required=True,type=Path)
    parser.add_argument('--user',default='wxbridge')
    parser.add_argument('--allow',required=True,action='append')
    args=parser.parse_args()
    try:install(args.wheel,args.user,args.allow);return 0
    except Exception as exc:
        print('native_helper_install_failed:'+type(exc).__name__,file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
