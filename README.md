# 微信适配与模型调用

源码分为三个职责：

| 模块 | 作用 | 当前文档 |
| --- | --- | --- |
| provider | OpenAI、Anthropic、DeepSeek 的同步调用、路由及图片生成 | [用法](docs/PROVIDER.md) |
| wechat_linux | Linux 微信数据库、消息、媒体、原生提交与版本分析 | [状态和调用方式](docs/WECHAT_LINUX.md) |
| wxbridge | 账号协调、群/私聊实例、模型通道、业务队列、记忆和发送确认 | [代码地图](src/wxbridge/README.md) |

## 从哪里开始

- 调模型：安装本地 model-provider 包，配置 provider 的 .env，使用 create_client() 和 ModelRequest。
- 接 Linux 微信：先看能力表和 Python 示例。原生图片发送已有代码，当前仍未完成实机验证。
- 调试或升级微信：[历史参考资料](docs/reference/README.md) 保留逆向分析、版本验证、部署和故障记录；现行接口以两份当前文档为准。

## 配置位置

| 文件 | 用途 |
| --- | --- |
| config.json | 本地账号、数据库和运行资源，Git 忽略 |
| `instance/<id>/config.json` | 独立群/私聊实例；enabled 控制启用，chat 使用稳定 ID |
| `instance/<id>/system.md` | 可选的实例系统提示词 |
| src/provider/.env | 模型连接、密钥和代理，Git 忽略 |
| config.defaults.json | 程序默认配置 |
| examples/account.config.example.jsonc | 账号配置示例 |
| examples/instance/config.example.jsonc | 实例配置示例 |

各实例的 reply/image/background 通道持有自己的模型 client，首次使用时创建并复用。同一通道串行，不同通道和实例可并行。账号层共享读库和发送确认协调。

## 开发入口

Python 要求 3.11+。正式微信运行依赖 Linux，原生档案针对微信 4.1.13.26 x86_64。当前源码发行版本：wechat-linux-adapter 0.2.0、model-provider 0.3.1。

安装开发依赖使用 python -m pip install -r requirements-test.txt，并安装本地 provider 包。直接从源码执行模块前，将仓库 src 加入 PYTHONPATH，具体命令见两份当前文档。

仓库检查入口为 python scripts/check-repository.py；需要运行离线测试时使用 python -X utf8 scripts/test-offline.py。真实接口及微信收发由使用方显式执行。

[脚本入口](scripts/README.md) · [开发约定](CONTRIBUTING.md) · [安全与隐私](SECURITY.md)

许可证暂未选定，目前用于受邀协作。密钥、私有实例配置、账号数据、聊天、运行目录和记忆不随源码发布。
