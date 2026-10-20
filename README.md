# 微信 + DeepSeek 项目

本项目让无桌面 Linux 主机上的原生微信读取普通群消息，在真实 @ 时调用 DeepSeek，再通过客户端发回群聊。

**双击 `Open-Project.cmd` 打开 VS Code。先看 [完整流程与代码导读](wechat-group-bot/docs/CODE_GUIDE.md)，再看 [运行说明](wechat-group-bot/docs/DEPLOYMENT.md)。**

```text
wechat-group-bot/
  wxbridge/       根目录入口/配置 + app、ai、wechat、storage 四组模块
  scripts/        微信会话启停、登录、API Key 配置、既有服务入口
  tests/          离线回归测试
  docs/           完整流程与运行说明
archives/         逆向研究、安装样本、历史脚本和整理前源码
.venv/            已有 Windows 测试环境
```

Git 跟踪当前源码、说明和配置空示例；`archives/`、虚拟环境、真实 `.env` / `config.json`、密钥及运行数据仅在本地保留，不纳入版本库。

已加入按账号、群和成员 wxid 隔离的人物记忆：确认回复后后台整理，下次提问使用；支持遗忘、纠正和自然的关系态度。私有 `memory/` 不纳入 Git，结构和维护见 [记忆说明](wechat-group-bot/docs/MEMORY.md)，本轮上线见 [部署记录](archives/deployments/20261020-memory/README.md)。

当前保留完整的“准备 → 登录 → 验证 → 监听 → 检查 → 停止”流程。重复的 UI 消息接收机器人、一次性回复脚本和其他研究路线放入归档；正常发送需要的 AT-SPI/X11，以及环境与 UI 排障能力保留。

在本目录的 PowerShell 运行离线验证：

```powershell
.\Test-Offline.ps1
```

默认模型 `deepseek-flash`，当前按用户要求开启思考、深度 `high`；真实 @ 触发，按消息发送时间取十分钟上下文。支持引用图片和按需联网搜索，搜索时发送一次口语提示，最终回答附一条来源网址。回复默认简短、保持人格，使用纯文本并清理意外 Markdown。Windows 修改不会自动同步，Linux 仅在用户明确授权部署时更新；搜索和输出偏好已上线，记录见 [部署记录](archives/deployments/20261020-websearch/README.md)，high 思考试用见 [切换记录](archives/deployments/20261020-thinking-high/README.md)。

新的目录地图见 [wxbridge/README.md](wechat-group-bot/wxbridge/README.md)，本次备份、路径对照与验证见 [目录重组记录](archives/refactors/20261020-package-layout/README.md)。

旧版说明、介质和反编译项目见 [归档索引](archives/README.md)，此次改动与恢复依据见 [整理记录](archives/core-cleanup-20261020/README.md)。
