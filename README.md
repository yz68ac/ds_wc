# 微信群聊 × DeepSeek

在 Linux 上用原生微信接收和发送群消息，用 DeepSeek 生成回答。主进程轮询本机数据库，真实 @ 独立排队；模型与界面操作使用工作进程，发送结果以新增本人发出记录确认。

项目目前用于受邀协作，许可证暂未选定。Windows 支持编辑和离线测试，完整运行依赖 Linux 微信、X11 与 AT-SPI。已有实机验证针对 Debian 13 amd64、微信 4.1.13.26；客户端升级需要重新核对适配。拍一拍解析仍属实验功能，实时同步尚未稳定验收。

## 功能

- 真实 @ 触发、默认五分钟上下文、队列恢复、冷却等待与逐片发送确认
- 引用文字和本地图片理解，按需联网搜索及一次口语等待提示
- 独立人格提示词、短答与纯文本偏好、可选微信内置表情
- 按稳定成员 ID 隔离的记忆、关系与语境表达学习
- 可选主动接话，以及无桌面 Linux 的会话和开机恢复脚本

## 先跑离线测试

需要 Git、Python 3.13 或 3.14。测试使用合成数据，不需要微信账号、API Key、SSH 或服务器。

```bash
git clone https://github.com/yz68ac/ds_wc.git
cd ds_wc
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -X utf8 scripts/test-offline.py
.\.venv\Scripts\python.exe scripts/check-repository.py --history
```

Linux：

```bash
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -X utf8 scripts/test-offline.py
.venv/bin/python scripts/check-repository.py --history
```

Linux 的原生图片解码回归还需要 `libde265` 和 FFmpeg 运行库；Debian/Ubuntu 可用 `sudo apt install libde265-0 ffmpeg`。Windows 按平台跳过 Linux 专用检查。Python 运行依赖范围见 `requirements.txt`，离线验证使用 `requirements-test.txt` 中的固定版本。

VS Code 打开仓库目录或 `wechat-group-bot.code-workspace`，选择仓库内 `.venv` 解释器即可。Windows 也可用 `Open-Project.cmd` 和 `Test-Offline.ps1`。

## 目录与代码入口

```text
wxbridge/
  cli.py、config.py       命令入口与配置加载
  app/                    轮询、调度、工作进程与诊断
  ai/                     模型、提示词、搜索、图片和输出
  wechat/                 数据库、消息解析、解密与界面
  storage/                事务队列、人物记忆和语境学习
prompts/                  人格、后台整理提示词与表情资料
scripts/                  离线检查、Linux 会话与运维脚本
tests/                    合成数据回归
docs/                     配置、架构和部署说明
.github/                  自动检查与协作模板
```

建议从 `wxbridge/cli.py → app/runtime.py → app/bot.py` 沿一次收发阅读，再进入具体模块。详细职责见 [代码导读](docs/CODE_GUIDE.md) 和 [包内地图](wxbridge/README.md)。

## 配置与实际运行

| 文件 | 用途 |
| --- | --- |
| `config.defaults.json` | 程序默认值，必须随代码分发 |
| `config.example.jsonc` | 带注释参考，不能直接作为标准 JSON 配置加载 |
| `config.json` | 本机实际覆盖配置，未填写字段继承默认值，Git 忽略 |
| `.env` | 仓库根目录的 API Key，Git 忽略；空模板为 `.env.example` |
| `prompts/system.md` | 群聊人格，每次生成重新读取 |
| `prompts/memory.md` | 后台整理提示词 |
| `memory/` | 本机私有记忆，Git 忽略 |

初次配置按 [部署说明](docs/DEPLOYMENT.md) 准备专用用户、原生微信、私有 `tmpfs,noswap`、会话与账号绑定。完整运行不能仅靠 `pip install`。配置项变更通常需重启监听；同一路径的人格文件修改从下一次生成生效。

`python -m wxbridge run --dry-run` 关闭发送，仍可能调用 API；纯离线检查使用上面的测试脚本。`run --send` 显式启用发送。不要同时启动两个监听器或绕过草稿、目标群和发送确认校验。常规状态检查使用 `queue-status`，不输出正文。

## 文档

| 内容 | 入口 |
| --- | --- |
| 配置与启动 | [配置项](docs/CONFIG.md)、[Linux 部署](docs/DEPLOYMENT.md)、[开机恢复](docs/BOOT_RECOVERY.md) |
| 代码与排障 | [完整流程](docs/CODE_GUIDE.md)、[常见问题](docs/TROUBLESHOOTING.md) |
| 模型功能 | [引用](docs/REFERENCES.md)、[图片](docs/VISION.md)、[搜索](docs/SEARCH.md)、[表情](docs/EMOTICONS.md) |
| 记忆与主动接话 | [人物记忆](docs/MEMORY.md)、[语境学习](docs/LEARNING.md)、[主动接话](docs/PROACTIVE.md) |
| 参与开发 | [贡献流程](CONTRIBUTING.md)、[安全与隐私](SECURITY.md) |

提交前检查暂存内容及 Git 历史，不上传真实配置、聊天、账号 ID、数据库、登录状态或记忆。GitHub Actions 仅运行离线回归，不连接生产服务器、不自动部署。
