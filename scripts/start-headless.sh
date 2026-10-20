#!/usr/bin/env bash
# 在无物理桌面的 Linux 上启动一个独立的 Xvfb 会话并运行微信。
# 用法：
#   ./start-headless.sh            # 启动 Xvfb + dbus + at-spi + fluxbox + 微信
#   ./start-headless.sh --vnc      # 同时启动 x11vnc（仅本机 5900，用 SSH 隧道访问）
#   ./start-headless.sh --no-wechat
#
# 之后用：
#   DISPLAY=:99 python3 -m wxbridge check
#   DISPLAY=:99 python3 -m wxbridge run
set -euo pipefail

DISPLAY_NUM="${WX_DISPLAY:-:99}"
SCREEN="${WX_SCREEN:-1280x800x24}"
WECHAT_BIN="${WECHAT_BIN:-/opt/wechat/wechat}"
RUNTIME="${WX_RUNTIME_DIR:-/tmp/wxbridge-$(id -u)}"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$RUNTIME/xdg}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export XAUTHORITY="${XAUTHORITY:-$RUNTIME/Xauthority}"
if [ ! -s "$XAUTHORITY" ]; then
  touch "$XAUTHORITY"
  chmod 600 "$XAUTHORITY"
  xauth -f "$XAUTHORITY" add "$DISPLAY_NUM" . "$(mcookie)"
fi

START_VNC=0
START_WECHAT=1
for arg in "$@"; do
  case "$arg" in
    --vnc) START_VNC=1 ;;
    --no-wechat) START_WECHAT=0 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

log() { echo "[wxbridge] $*"; }

# ---------- Xvfb ----------
if [ -f "$RUNTIME/xvfb.pid" ] && kill -0 "$(cat "$RUNTIME/xvfb.pid")" 2>/dev/null; then
  log "Xvfb 已在运行：$DISPLAY_NUM"
else
  log "启动 Xvfb $DISPLAY_NUM ($SCREEN)"
  Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp -auth "$XAUTHORITY" >"$RUNTIME/xvfb.log" 2>&1 &
  echo $! > "$RUNTIME/xvfb.pid"
  sleep 1
fi
export DISPLAY="$DISPLAY_NUM"

# ---------- D-Bus session ----------
if [ -f "$RUNTIME/dbus.env" ]; then
  # shellcheck disable=SC1090
  source "$RUNTIME/dbus.env"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] || ! dbus-send --session --dest=org.freedesktop.DBus --type=method_call --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
  log "启动 D-Bus session"
  dbus-launch --sh-syntax > "$RUNTIME/dbus.env"
  # shellcheck disable=SC1090
  source "$RUNTIME/dbus.env"
fi
export DBUS_SESSION_BUS_ADDRESS

# 给 systemd 用户服务用的环境文件（systemd 不解析 export 语法）
{
  echo "DISPLAY=$DISPLAY"
  echo "XAUTHORITY=$XAUTHORITY"
  echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
  echo "QT_ACCESSIBILITY=1"
  echo "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1"
  echo "GTK_MODULES=${GTK_MODULES:-gail:atk-bridge}"
} > "$RUNTIME/env"
chmod 600 "$RUNTIME/env"

# ---------- AT-SPI ----------
export GTK_MODULES="${GTK_MODULES:-gail:atk-bridge}"
export QT_ACCESSIBILITY=1
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export NO_AT_BRIDGE=0
if [ -f "$RUNTIME/atspi.pid" ] && kill -0 "$(cat "$RUNTIME/atspi.pid")" 2>/dev/null; then
  log "at-spi 已在运行"
else
  ATSPI_BIN="$(command -v at-spi-bus-launcher 2>/dev/null || true)"
  if [ -z "$ATSPI_BIN" ]; then
    for p in /usr/libexec/at-spi-bus-launcher \
             /usr/lib/at-spi2-core/at-spi-bus-launcher \
             /usr/lib/at-spi2/at-spi-bus-launcher; do
      [ -x "$p" ] && ATSPI_BIN="$p" && break
    done
  fi
  if [ -n "$ATSPI_BIN" ]; then
    log "启动 at-spi：$ATSPI_BIN"
    "$ATSPI_BIN" >"$RUNTIME/atspi.log" 2>&1 &
    echo $! > "$RUNTIME/atspi.pid"
  else
    log "未找到 at-spi-bus-launcher；Qt 启动时会尝试 D-Bus 自动拉起"
  fi
fi

# ---------- 窗口管理器 ----------
if [ -f "$RUNTIME/fluxbox.pid" ] && kill -0 "$(cat "$RUNTIME/fluxbox.pid")" 2>/dev/null; then
  log "fluxbox 已在运行"
elif command -v fluxbox >/dev/null 2>&1; then
  log "启动 fluxbox"
  fluxbox >"$RUNTIME/fluxbox.log" 2>&1 &
  echo $! > "$RUNTIME/fluxbox.pid"
else
  log "未安装 fluxbox（可选）；没有窗口管理器时 xdotool 激活窗口可能不稳定"
fi

# ---------- x11vnc（可选，仅本机） ----------
if [ "$START_VNC" = "1" ]; then
  if ! command -v x11vnc >/dev/null 2>&1; then
    log "未安装 x11vnc。sudo apt install x11vnc"
  elif [ -f "$RUNTIME/vnc.pid" ] && kill -0 "$(cat "$RUNTIME/vnc.pid")" 2>/dev/null; then
    log "x11vnc 已在运行"
  else
    log "启动 x11vnc：仅监听 127.0.0.1:5900（请用 SSH 隧道）"
    x11vnc -display "$DISPLAY" -localhost -nopw -forever -shared -rfbport 5900 \
      >"$RUNTIME/vnc.log" 2>&1 &
    echo $! > "$RUNTIME/vnc.pid"
  fi
fi

# ---------- 微信 ----------
if [ "$START_WECHAT" = "1" ]; then
  if ! [ -x "$WECHAT_BIN" ]; then
    log "找不到微信可执行文件：$WECHAT_BIN（可用 WECHAT_BIN=... 指定）"
    exit 1
  fi
  if [ -f "$RUNTIME/wechat.pid" ] && kill -0 "$(cat "$RUNTIME/wechat.pid")" 2>/dev/null; then
    log "微信已在运行：PID $(cat "$RUNTIME/wechat.pid")"
  else
    log "启动微信（QT_QPA_PLATFORM=xcb）"
    QT_QPA_PLATFORM=xcb \
    QT_ACCESSIBILITY=1 \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    QT_AUTO_SCREEN_SCALE_FACTOR=0 \
    QT_ENABLE_HIGHDPI_SCALING=0 \
    QT_SCALE_FACTOR=1 \
    GTK_MODULES=gail:atk-bridge \
    DISPLAY="$DISPLAY" \
    DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    nohup "$WECHAT_BIN" >"$RUNTIME/wechat.log" 2>&1 &
    echo $! > "$RUNTIME/wechat.pid"
    sleep 2
  fi
fi

log "完成。DISPLAY=$DISPLAY"
log "日志目录：$RUNTIME"
if [ "$START_VNC" = "1" ]; then
  log "VNC：ssh -L 5900:127.0.0.1:5900 用户@这台机器 后连接 localhost:5900"
fi
log "检查：DISPLAY=$DISPLAY python3 -m wxbridge check"
