# 同一微信账号的多群实例

一个监听服务管理多个群实例。账号层共享登录客户端、显示环境、数据库密钥、唯一读库入口、原生 helper 和模型并发额度；每群拥有独立人格、模型角色、上下文、接收游标、任务队列、参与窗口、关系记忆和状态。当前是逻辑实例，服务重启会一起恢复所有群；并非每群一个 systemd 服务，也不支持在同一群配置多个实例。

## 配置

`config.json` 的根配置仍是共享设置与各群的默认值。`instances` 空列表保持原单群行为；非空时只监听其中启用的群，根 `target.chat` 不再额外启动一个群。最多16项；`id` 不区分大小写去重，允许字母、数字、下划线和短横线，最长48字符。`chat` 必须为稳定的 `…@chatroom` ID，不能填会改名或重名的群标题。

以下是需要合入实际配置的片段，示例 ID 要换成自己的真实群 ID。先准备第二个人格文件；`openai` 和 `deepseek_chat` 引用默认配置已有的模型连接，API Key 继续放根目录 `.env`。

```json
{
  "ui": {"backend": "native_service"},
  "native_api": {
    "enabled": true,
    "allowed_chat_ids": ["10001@chatroom", "10002@chatroom"]
  },
  "account": {"send_interval_seconds": 1},
  "instances": [
    {
      "id": "main",
      "chat": "10001@chatroom",
      "reuse_legacy_state": true,
      "overrides": {
        "persona": {
          "name": "ds",
          "aliases": ["小D"],
          "prompt_file": "prompts/system.md"
        },
        "models": {
          "reply": {"profile": "openai", "thinking": "enabled", "reasoning_effort": "medium"}
        }
      }
    },
    {
      "id": "friends",
      "chat": "10002@chatroom",
      "overrides": {
        "persona": {
          "name": "小夏",
          "aliases": ["夏夏"],
          "prompt_file": "prompts/friends.md"
        },
        "models": {
          "reply": {"profile": "deepseek_chat"},
          "memory": {"profile": "deepseek_chat", "thinking": "enabled", "reasoning_effort": "max"},
          "participation": {"profile": "deepseek_chat", "thinking": "disabled"}
        },
        "search": {"backend": "external"},
        "proactive": {"enabled": false},
        "target": {"context_window_seconds": 300}
      }
    }
  ]
}
```

每群可覆盖 `persona`、`target`（除 `chat`）、`deepseek`、`models`、`model_profile`、`reply_style`、`proactive`、`memory`（除 `directory`）、`search`、`vision`、`queue`（除 `file`、`generation_concurrency`）。其余字段是账号级资源，不接受实例覆盖。模型 `reply/memory/participation/avatar` 均可独立选择；显式选择另一个角色 `profile` 会清除该角色从根配置继承的模型、代理、思考等覆盖值，再采用新 profile 和本次显式参数，避免旧供应商参数残留。

`persona.prompt_file` 相对 `config.json` 所在目录解析。记忆整理提示词保持原有 `memory.system_prompt_file` 路径规则，可分别覆盖。改变配置或启用列表需要重启监听；同路径人格文件的内容仍在每次生成时读取。多群模式拒绝 `WX_GROUP` 环境变量，避免旧单群覆盖误导路由。

真实发送要求 `ui.backend=native_service`，图片自动下载开启时要求 `vision.download_backend=native`。必须同时把新群加入应用和 root helper 的允许清单；机器人不会自行扩大 helper 权限。真实 @ 仍识别同一个微信账号 ID；各群的文字名称触发可独立设置，手机显示的账号头像不会随人格改变。

## 从原单群切换

1. 先准备新配置与人格文件，并核对稳定群 ID；此时不要启动第二个监听进程。
2. 停止唯一监听。原来的群设置 `reuse_legacy_state=true`，保留根 `target.chat` 指向原群。程序会验证它确实是同一个群。
3. 该实例沿用原队列、状态路径和开机游标检查点，已接纳任务、未知发送意图、接收游标均保留。只能一个实例沿用旧状态。
4. 新群默认从首次启动时的最新记录建立游标，不补答旧历史。新群的运行文件放在根队列同目录的 `instances/<id>/`，仍强制私有 `tmpfs,noswap` 与0600权限。

记忆继续按“账号＋群”哈希隔离存放在原 `memory/` 目录；沿用原群不会重建人物档案。同一个人在不同群的好感和记忆互不混用。不要把同一个 `id` 改绑另一个群来复用旧队列；账号、群或已记录的实例 ID 不匹配会拒绝恢复。

`enabled=false` 暂停该群监听，不删除队列和记忆；下次启用恢复原任务。暂停期间尚未接纳的消息，恢复后仍受消息接收时效限制。不要删除未决发送所属的实例或改名其 `id`；它必须留在配置中，未知发送结果仍会阻止账号提交新消息，直到重新启用后核对清楚。

## 调度和故障边界

- 所有微信查询和解密刷新都由主进程的同一个 Store 执行。模型工作进程只接收所属群快照；不会自己查库。
- 根 `queue.generation_concurrency` 是整个账号的上限，仍为1或2。前台生成轮流获取空位；不会给每个群各开两个请求。后台记忆最多一个，必要时暂停并重新排队；名称呼叫的介入判断不会因为另一群的新提问而被静默丢弃。
- 发送在账号层串行调度。`account.send_interval_seconds` 限制不同群之间的间隔，每群另有 `target.cooldown_seconds`。发送前补读所有启用群，并重新检查优先级和任务版本。明确提问优先于主动插话。
- 每个分片仍先保存意图，再提交，再核对目标群新增的本人正文记录。相同正文出现在另一个群不能确认当前任务。结果不明时整个账号停止新发送，各群继续接收、排队和整理已完成的模型结果；不自动重发或回退桌面。
- 一个服务、一个本地 API socket、一个 helper。外部请求按已启用实例的群 ID 路由，请求 ID 在账号内检查冲突；不接纳没有启用实例的收件人。`filehelper` 等单群时代额外目标在多群模式下不自动借用别人的队列。
- 一个群的网络或模型错误进入自己的重试队列。主进程、微信客户端和 helper 的故障仍是账号级故障；需要进程级独立故障恢复时再扩展本地 IPC。

## 查看与维护

```bash
cd /opt/wxbridge
python3 -m wxbridge status --once
python3 -m wxbridge queue-status
python3 -m wxbridge --instance main status
python3 -m wxbridge --instance friends queue-status
python3 -m wxbridge --instance friends memory status
python3 -m wxbridge --instance friends check
```

以上命令须以现有运行用户执行。`check` 会查询微信数据库，需要先停监听并取得同一把读库锁；查看运行中的情况使用 `status` 或 `queue-status`。未选实例的 `status` 在多群模式下输出一次账号汇总；选实例后使用原只读交互面板。每群状态保存在对应运行目录的 `live-status.json`，账号汇总为根状态目录的 `account-status.json`。日志附带实例 ID，并可在所选实例面板中过滤，不写入提示词、聊天正文或密钥。

修改记忆需要停止整个监听，维护命令继续取得相同的读库锁和该群队列锁。`--instance` 不能用于 `run`，避免误开另一套读库所有者；原启动命令自动管理所有启用实例。开机恢复逐群验证账号、群 ID 和游标；发布检查汇总全部群的待处理任务与记忆，等待账号空闲后切换。

本轮通过合成群的隔离、并发、发送确认、重启和检查点测试；多群真实收发仍需提供第二个目标群后验收。
