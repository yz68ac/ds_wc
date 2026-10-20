#!/usr/bin/env bash
# 停止 start-headless.sh 启动的进程（只按 PID 文件停止，不碰其他进程）。
set -euo pipefail
RUNTIME="${WX_RUNTIME_DIR:-/tmp/wxbridge-$(id -u)}"

stop_one() {
  local name="$1" file="$RUNTIME/$1.pid"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "[wxbridge] 停止 $name (PID $pid)"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_one wechat
stop_one vnc
stop_one fluxbox
stop_one atspi
stop_one xvfb

echo "[wxbridge] 已停止。"
