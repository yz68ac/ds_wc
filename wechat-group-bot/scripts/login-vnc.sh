#!/usr/bin/env bash
# 需要在浏览器/手机上看二维码时，临时启动 x11vnc；登录完成后 Ctrl+C 或 kill 即可。
# 只监听 127.0.0.1，推荐用 SSH 隧道：
#   ssh -L 5900:127.0.0.1:5900 user@host
# 然后 VNC 客户端连接 localhost:5900。
set -euo pipefail
DISPLAY_NUM="${WX_DISPLAY:-:99}"
RUNTIME="${WX_RUNTIME_DIR:-/tmp/wxbridge-$(id -u)}"
if [ -f "$RUNTIME/env" ]; then
  set -a
  source "$RUNTIME/env"
  set +a
fi
export XAUTHORITY="${XAUTHORITY:-$RUNTIME/Xauthority}"
test -s "$XAUTHORITY" || { echo '未找到当前微信会话的 Xauthority，请先启动会话。' >&2; exit 1; }
if ! command -v x11vnc >/dev/null 2>&1; then
  echo "请先安装：sudo apt install x11vnc" >&2
  exit 1
fi
echo "x11vnc 监听 127.0.0.1:5900，DISPLAY=$DISPLAY_NUM；Ctrl+C 结束。"
exec x11vnc -display "${DISPLAY:-$DISPLAY_NUM}" -auth "$XAUTHORITY" -localhost -nopw -forever -shared -rfbport 5900
