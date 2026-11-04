ARG BASE_IMAGE=debian:13-slim@sha256:1e1a22175fbf1b32157ddaf0219e1db4e5401fab2b94ed285add64d18587cd2d
FROM ${BASE_IMAGE}
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv python3-pip python3-gi python3-pycryptodome python3-zstandard gir1.2-atspi-2.0 gir1.2-gtk-3.0 at-spi2-core xvfb xauth dbus-x11 fluxbox xdotool libde265-0 ffmpeg ca-certificates curl nodejs npm && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/opt/wxbridge/src
RUN apt-get update && apt-get install -y --no-install-recommends libnss3 libnspr4 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/wxbridge
COPY . .
RUN python3 -c "from pathlib import Path; [(p.write_bytes(p.read_bytes().replace(bytes([13,10]),bytes([10])))) for p in Path('scripts').rglob('*.sh')]"
RUN python3 -c "import runpy; from pathlib import Path; runpy.run_path('src/wxbridge_cli/install.py')['verify_media'](Path('.'),Path('resources/wechat/linux-x86_64/4.1.13.26/installer/WeChatLinux_x86_64.deb'))"
RUN apt-get update && apt-get install -y ./resources/wechat/linux-x86_64/4.1.13.26/installer/WeChatLinux_x86_64.deb && rm -rf /var/lib/apt/lists/* resources/wechat/linux-x86_64/4.1.13.26/installer
RUN cd src/web/frontend && npm ci --no-audit --no-fund && npm run build && rm -rf node_modules
RUN python3 -m venv --system-site-packages .venv && .venv/bin/pip install uv==0.12.24 && .venv/bin/uv sync --locked --extra database --extra login --no-dev --python /usr/bin/python3
ENV PYTHONPATH=/opt/wxbridge/src
RUN groupadd -g 10001 wxbridge && useradd -u 10001 -g 10001 -d /var/lib/wxbridge -M wxbridge && .venv/bin/python -c "import runpy; from pathlib import Path; d=runpy.run_path('src/wxbridge_cli/install.py'); d['fetch_frida'](Path('/tmp/frida.whl'),runpy.run_path('scripts/install-native-helper.py')['WHEEL_SHA256'],proxy=__import__('os').environ.get('HTTPS_PROXY'))" && .venv/bin/python scripts/install-native-helper.py --wheel /tmp/frida.whl --allow filehelper && .venv/bin/python -c "from importlib.resources import files; import shutil; [shutil.copyfile(p, '/opt/wxbridge-native-helper/'+p.name) for p in files('wechat_linux.native').iterdir() if p.name.startswith('image') and p.is_file()]" && rm /tmp/frida.whl
RUN mkdir -p /usr/local/lib/wxbridge-recovery && cp -r src/wxbridge /usr/local/lib/wxbridge-recovery/ && .venv/bin/python -c "from pathlib import Path; import shutil,wechat_linux; shutil.copytree(Path(wechat_linux.__file__).parent, '/usr/local/lib/wxbridge-recovery/wechat_linux', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))" && ln -s /opt/wxbridge/.venv/bin/wxbridge /usr/local/bin/wxbridge
RUN .venv/bin/python -c "import hashlib,json; from pathlib import Path; from importlib.resources import files; expected=json.loads(files('wechat_linux.native').joinpath('profile.json').read_text())['sha256']; assert hashlib.file_digest(open('/opt/wechat/wechat','rb'),'sha256').hexdigest()==expected"
ENV WXBRIDGE_RUNTIME=container
ENTRYPOINT ["/opt/wxbridge/.venv/bin/python", "-m", "wxbridge.host.container"]
