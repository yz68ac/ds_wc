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

当前保留完整的“准备 → 登录 → 验证 → 监听 → 检查 → 停止”流程。重复的 UI 消息接收机器人、一次性回复脚本和其他研究路线放入归档；正常发送需要的 AT-SPI/X11，以及环境与 UI 排障能力保留。

在本目录的 PowerShell 运行离线验证：

```powershell
.\Test-Offline.ps1
```

默认模型 `deepseek-flash`、关闭思考；真实 @ 触发，按消息发送时间取十分钟上下文。**本次目录重组只在 Windows 本地完成；Linux 保持此前已运行的图片理解与人格版本，后续由用户明确要求更新。**

新的目录地图见 [wxbridge/README.md](wechat-group-bot/wxbridge/README.md)，本次备份、路径对照与验证见 [目录重组记录](archives/refactors/20261020-package-layout/README.md)。

旧版说明、介质和反编译项目见 [归档索引](archives/README.md)，此次改动与恢复依据见 [整理记录](archives/core-cleanup-20261020/README.md)。
