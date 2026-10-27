# 后台服务文本发送接口

当前适配 Linux x86-64 微信 4.1.13.26。选择 `ui.backend=native_service` 后，普通 @、关键词、主动接话、搜索反馈和 CLI 请求共用现有队列，通过客户端后台服务向明确 chat_id 发送。发送不读取输入框、不切会话、不点击按钮，也不扫描堆内存寻找聊天视图。

```text
外部 CLI / NativeClient → 私有 Unix Socket → 现有主进程 / 内存持久队列
    → 串行发送工作进程：明确收件人、持久发送意图
    → 隔离权限的原生 helper → Frida / 微信主线程：构造文本源、创建并订阅批次任务
    → 微信自己的工作线程：核心消息处理
    → 主进程核对新增本人发出记录 → confirmed
```

正文与收件人直接构造为新消息源，复用客户端调度和引用计数。现有草稿不被触碰，也不会阻挡后台提交；必须已有可读的目标消息表用于确认。微信客户端登录与运行仍保留显示环境，本功能不承诺移除 X11。此次没有实现任意离线联系人、图片、引用或收藏表情发送。

两轮对象与后台链路分析、验证边界见[分析总结图](NATIVE_ANALYSIS.md)。

`client.py`、主进程接口、隔离helper和进程内 `agent.js` 的职责，以及脚本如何加载到微信，见 [native代码地图](../wxbridge/native/README.md)。

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

群聊必须使用内部 ID，例如 `123456789@chatroom`，并事先同时加入应用和 helper 的允许清单。`info` 只读显示本机允许目标、字节上限和普通回复后端，不会自动发送测试消息。

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
- 普通回复的每个分片/反馈在发送意图事务内保存独立的随机 `bot:` 请求 ID；进程恢复不会重新提交未决意图，同一分片确认后才推进下一片。
- 监听进程重启保留任务、未决意图和去重结果。任务完成清除正文，裁剪完成任务后仍保留去重元数据。
- 去重记录放在现有私有 tmpfs,noswap 队列中，**主机重启会清空**。不能把此接口当作跨主机重启的永久幂等账本。
- 达到历史 ID 上限后拒绝新请求，不自动淘汰旧 ID 以免重发。应在明确理解主机生命周期的前提下维护请求历史。
- 上一条指外部 API 的主进程去重账本。helper 只有最多4096条的易失 LRU 回放缓存，会随重启/淘汰消失；跨 helper 重启和长期运行的防重由主队列意图负责，不能把内部 socket 当作独立持久去重接口。

## 应用配置

在实际 `config.json` 中覆盖，重启监听生效：

```json
{
  "ui": {"backend": "native_service"},
  "native_api": {
    "enabled": true,
    "socket": "/run/wxbridge-trial/native-api.sock",
    "helper_socket": "/run/wxbridge-trial/native-helper.sock",
    "allowed_chat_ids": ["filehelper", "123456789@chatroom"],
    "max_text_bytes": 4096,
    "max_pending": 16,
    "max_requests": 1024
  }
}
```

把示例群 ID 替换为实际配置群。发行默认仍为 `ui.backend=auto`、外部接口关闭，便于尚未安装 helper 的实例；当前已安装实例选择 `native_service` 后不会自动回退桌面。配置后端或允许清单需要重启监听，helper 的允许清单也需单独同步并重启 helper。

外部请求按 UTF-8 字节数限制，保留换行和原文，不使用模型回复的 Markdown 清理或末尾标点规则；单次外部请求对应一条文字，不自动拆分。普通生成回复先保留既有自然段/字符分片，再只对尚未提交的内容按字节上限细分，保留 UTF-8 字符及短方括号表情标记，不改正文、不移动已确认的分片。无法表达的分片在提交前阻塞并说明固定错误码，不截断或丢字。

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

原生对象和字符串布局只对当前样本有效，升级后拒绝提交。服务每轮从全局注册表读取并验证，文本源新建；不再缓存聊天视图/发送控制器，也不扫描512 MiB堆内存。仍有进程定位、样本 hash 核验与短时 Frida 连接开销，未测量整机 CPU 或风控变化，不能据此承诺降低封号风险。

两条发送通道共用队列、冷却、唯一发送工作进程和数据库确认。接口空闲时不额外查询微信数据库，也不启动状态面板刷新。原生未知结果不自动重发；日志仅保存错误码、长度、任务和确认元数据。
