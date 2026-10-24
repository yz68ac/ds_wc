# 实验性原生文本发送接口

当前适配 Linux x86-64 微信 4.1.13.26。接口通过现有监听主进程接收请求并入队；只有显式提交到本接口的文字使用内部发送函数，普通 @、关键词、主动接话仍使用原来的桌面发送。

```text
外部 CLI / NativeClient → 私有 Unix Socket → 现有主进程 / 内存持久队列
    → 串行发送工作进程：选会话、检查空草稿
    → 隔离权限的原生 helper → Frida / 微信主线程 → 内部输入模型 Send
    → 主进程核对新增本人发出记录 → confirmed
```

正文直接构造为原生消息对象，不输入、不粘贴正文，也不点击发送按钮。目标会话选择和草稿检查仍使用 AT-SPI；必须已有可读的本地消息表。此次没有实现任意离线联系人、图片、引用或收藏表情发送。

## Linux CLI

部署并开启后，在 SSH 中使用 `ds-native`；root 入口自动切换为 wxbridge 用户。正文来自 stdin 或 UTF-8 文件，不放进进程命令行参数。

```bash
ds-native info

printf '%s\n' '原生接口测试：你好[偷笑]' '第二行也能保留' |
  ds-native send --chat-id filehelper --request-id my-native-test-001

ds-native status --request-id my-native-test-001
```

文件输入：

```bash
ds-native send --chat-id filehelper --request-id my-native-test-002 --text-file ./message.txt
```

没有安装快捷入口时：

```bash
cd /opt/wxbridge
runuser -u wxbridge -- python3 -m wxbridge native-api info
```

群聊必须使用内部 ID，例如 `123456789@chatroom`，并事先同时加入应用和 helper 的允许清单。`info` 只读显示本机配置的允许目标和字节上限。首次部署默认只允许文件传输助手，不会自动向群发测试消息。

## Python 调用

客户端不需要 Frida，也不直接读微信数据库。应以运行机器人的 Linux 用户连接私有 socket；Windows 端可以通过 SSH 调用上述 CLI，不开放公网端口。

```python
from wxbridge.native.client import NativeClient

client = NativeClient()
accepted = client.send_text(
    chat_id="filehelper",
    text="第一行中文😀\n第二行[偷笑]",
    request_id="my-native-test-003",
)
result = client.get_status("my-native-test-003")
```

受理返回 `request_id`、`task_id`、`state`、`duplicate` 等元数据，不返回正文。通常先为 `queued`；原生调用返回后仍为 `confirming`，只有目标会话中新增本人、类型为文字、正文完全相同的记录出现后才为 `confirmed`。确认不代表已读。

状态还包括 `failed`（明确未提交）、`blocked`（提交或确认状态不明）。发生 `blocked` 时保留发送意图并继续接收微信消息，暂停出队；不会切回桌面重发。

## 去重及恢复

- 同一次业务操作始终使用同一个 `request_id`，超时重试也不换 ID。同 ID、同目标和同正文返回原任务；同 ID 改正文或目标返回 `request_id_conflict`。
- 请求 ID 和任务在同一个 SQLite 事务中保存；不推进微信群接收游标，也不调用模型、修改人格格式或触发人物记忆整理。
- 监听进程重启保留任务、未决意图和去重结果。任务完成清除正文，裁剪完成任务后仍保留去重元数据。
- 去重记录放在现有私有 tmpfs,noswap 队列中，**主机重启会清空**。不能把此接口当作跨主机重启的永久幂等账本。
- 达到历史 ID 上限后拒绝新请求，不自动淘汰旧 ID 以免重发。应在明确理解主机生命周期的前提下维护请求历史。

## 应用配置

在实际 `config.json` 中覆盖，重启监听生效：

```json
{
  "native_api": {
    "enabled": true,
    "socket": "/run/wxbridge-trial/native-api.sock",
    "helper_socket": "/run/wxbridge-trial/native-helper.sock",
    "allowed_chat_ids": ["filehelper"],
    "max_text_bytes": 4096,
    "max_pending": 16,
    "max_requests": 1024
  }
}
```

默认关闭。文字按 UTF-8 字节数限制，保留换行和原文，不使用模型回复的 Markdown 清理或末尾标点规则；单次请求对应一条文字，不自动拆分。

## helper 安装与权限

特权 helper 不从 wxbridge 可写的项目目录导入 Python 代码或 Frida。安装器把 helper、代理、版本指纹及独立 Frida 环境放在 root 所有的 `/opt/wxbridge-native-helper`；使用 `python -I`，不读取项目 `.env`，不读微信数据库。

需要已有、核验过的官方 `frida-17.24.3-cp37-abi3-manylinux_2_5_x86_64.whl`。安装器核对整包 SHA-256 与 RECORD，复用系统 Python 创建 without-pip venv，不下载依赖，也不覆盖已有安装目录、服务或快捷命令。

```bash
python3 scripts/install-native-helper.py --wheel /path/to/verified-frida.whl --allow filehelper
systemctl daemon-reload
systemctl enable --now wxbridge-native-helper.service
```

应用 socket 为 wxbridge 所有、0600；helper socket 为 root 所有、机器人主组可访问，双方都检查 Unix peer UID。helper 配置及程序由 root 维护；增加目标时需同步 root 的 `helper-config.json` 并重启 helper。

helper 每次仅处理一个受限请求，先验证目标允许清单、进程 UID、主程序 hash 和关键函数指纹。客户端排队回调通过 GLib 单次 idle source 执行，并使用脚本 pin 保持回调存活至销毁；控制器超时后也不能留下失效的函数指针。

## 当前试验边界

原生对象和字符串布局只对当前样本有效，升级后拒绝提交。会话对象每轮重新定位并在调用前复核，不长期缓存裸指针；发现过程最多扫描 512 MiB 内存，仍是实验方案，不能据此承诺比桌面发送更省 CPU 或风控更低。

两条发送通道共用队列、冷却、唯一发送工作进程和数据库确认。接口空闲时不额外查询微信数据库，也不启动状态面板刷新。原生未知结果不自动重发；日志仅保存错误码、长度、任务和确认元数据。
