# Linux 微信 + DeepSeek 群聊桥接

原生微信负责登录、同步和实际发消息。本程序读取本机数据库，每次真实 @ 独立排队并立即调度生成，默认关闭连续 @ 合并。API 与 UI 使用工作进程，主进程继续接收并核对发出记录。

支持引用消息后真实 @ 提问，例如“@ds 这怎么看”：文字和链接快照单独提供；引用图片会读取本地缓存、内存解密，再随问题发送给模型理解。语音和视频暂不识别。见 [引用说明](docs/REFERENCES.md)和 [图片说明](docs/VISION.md)。

## 按完整流程读代码

`wxbridge` 按 `app`（应用调度）、`ai`（模型生成）、`wechat`（微信适配）、`storage`（状态存储）划分目录。详细文件地图见 [wxbridge/README.md](wxbridge/README.md)。Linux 上线状态与验证记录见 [运行说明](docs/DEPLOYMENT.md)。

| 阶段 | 入口与模块 |
| --- | --- |
| 配置 | `config.defaults.json` 默认值、`config.example.json` 自定义示例、`wxbridge/config.py` 加载与校验 |
| 人格设定 | `prompts/system.md`，由 `wxbridge/ai/prompts.py` 读取 |
| 启动微信、手机登录 | `scripts/start-headless.sh`、`scripts/login-vnc.sh` |
| 检查环境、准备数据库密钥 | `python -m wxbridge env`、`scan-keys`、`wxbridge/wechat/keyscan.py` |
| 确认目标群和收发组件 | `python -m wxbridge check` |
| 统一启动监听 | `wxbridge/cli.py → app/runtime.py → app/bot.py` |
| 读取与判断消息 | `wechat/store.py`、`wechat/crypto.py`、`storage/memoryfs.py`、`ai/context.py` |
| 调用模型、发送回复 | `ai/client.py`、`ai/prompts.py`、`ai/images.py`、`wechat/ui.py` |
| 检查、排障、停止 | `app/diagnostics.py`、运行状态文件、`scripts/stop-headless.sh` |

详细说明见 [完整流程与代码导读](docs/CODE_GUIDE.md)、[配置项说明](docs/CONFIG.md)和 [运行说明](docs/DEPLOYMENT.md)。

## 当前规则

- 模型 `deepseek-flash`，当前开启思考、深度 `high`，只发送最终 `content`，不发送内部推理。
- 最终回复确认后后台整理本次十分钟窗口的人物记忆，按账号、群和 wxid 隔离；下次提问使用，详见 [记忆说明](docs/MEMORY.md)。
- 需要新信息时按需调用 DeepSeek Search/Fetch，先发一次口语提示再给答案及真实来源；普通闲聊不搜索，详见 [搜索说明](docs/SEARCH.md)。
- 只回应真实 @ 自己的群聊文本；首次接收过滤过旧、未来或无效记录。已有队列在重启后恢复；新部署才建立基线。
- 私有内存 SQLite 同事务保存接收游标与任务，冷却只等待不丢弃；每个回复分片以新增本人发出记录确认，超时暂停发送并继续接收。
- 按触发消息数据库发送时间 `t` 查询 `[t-600, t]`，再次排除同秒较晚记录；超出字符预算取消回复。
- 发送前检查原生窗口、唯一群名、选中状态和输入框全文；结果不明不自动重发。
- 解密副本和密钥放私有 `tmpfs,noswap`，日志默认不保存聊天正文。

## 查看和测试

本目录就是 Git 仓库根目录，源码、配置示例和文档均从这里开始。Windows 双击 `Open-Project.cmd` 打开本目录工作区，或在这里运行 `Test-Offline.ps1`。脚本优先使用本目录 `.venv`，缺少时沿用上一层现有测试环境。其他已准备好 Python 依赖的环境在本目录运行：

```bash
python3 scripts/test-offline.py
python3 -m wxbridge --help
```

离线测试不访问微信、SSH 或 DeepSeek。`requirements.txt` 是 Python 库依赖，Linux 的 Xvfb、AT-SPI/PyGObject 等系统依赖需要另行准备。

## 运行入口

API Key 保存在本源码根目录的 `.env`，与本 README 同级，格式为 `DEEPSEEK_API_KEY=你的密钥`。真实文件已被 Git 忽略；`.env.example` 是可提交的空示例。程序始终从这个根目录读取 `.env`，不随运行目录或 `--config` 位置变化。Linux 对应 `/opt/wxbridge/.env`。

人格提示词独立存放在 [prompts/system.md](prompts/system.md)，由 `wxbridge/ai/prompts.py` 读取，配置使用 `deepseek.system_prompt_file` 指向它。可直接写较长的 Markdown、多段描述和对话示例；仅替换 `{bot_name}`，其他花括号无需转义。编辑同一文件后，下一次生成回复时读取新内容；旧版内联 `system_prompt` 配置仍兼容。

`config.example.json` 现在与实际数据库链路一致：`source: db`，运行数据默认指向 `/run/wxbridge-trial`，`dry_run: true`。本地没有从 Linux 复制真实配置或 API Key。

`python -m wxbridge run` 尊重配置中的 dry-run；`run --send` 显式启用当前进程发送，`run --dry-run` 关闭发送但仍可能调用模型，`--echo` 用固定回复代替模型。`scripts/run-live.py` 保留既有服务入口，转到同一个 CLI/runtime 流程并显式传入 `--send`。

已有实机验证针对 Debian 13.7 amd64、微信 4.1.13.26。当前没有自动安装、开机恢复和长期重连机制；这些边界在运行说明中单独列出。
