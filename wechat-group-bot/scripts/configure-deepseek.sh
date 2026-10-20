#!/usr/bin/env bash
# 在 SSH 终端交互输入；密钥不进入 shell 历史、进程参数或聊天。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" = 0 ]; then
  exec runuser -u wxbridge -- /usr/bin/bash "$ROOT/scripts/configure-deepseek.sh"
fi
cd "$ROOT"
if [ ! -t 0 ]; then
  echo '请用 ssh -t 交互运行此脚本，不通过命令行参数或聊天发送密钥。' >&2
  exit 1
fi
if [ -e .env ]; then
  echo '.env 已存在，未覆盖。请在服务器上自行编辑已有文件。' >&2
  exit 1
fi
umask 077
IFS= read -r -s -p 'DeepSeek API Key（输入隐藏）: ' wxbridge_api_key
printf '\n'
if [[ ! "$wxbridge_api_key" =~ ^[A-Za-z0-9_-]+$ ]]; then
  unset wxbridge_api_key
  echo '密钥为空或格式不符合预期，未保存。' >&2
  exit 1
fi
set -o noclobber
printf 'DEEPSEEK_API_KEY=%s\n' "$wxbridge_api_key" > .env
unset wxbridge_api_key
chmod 600 .env
echo '已保存至服务器本次项目的 .env（权限 600），未启动机器人。'
