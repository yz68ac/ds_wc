# 参与开发

目前用于受邀协作，许可证暂未选定。现行接口见 [provider 用法](docs/PROVIDER.md) 和 [wechat_linux 状态](docs/WECHAT_LINUX.md)，实验与运维记录见 [历史参考](docs/reference/README.md)。

## 模块职责

- provider 处理模型协议和 SDK 调用；使用方持有 client 并负责并发、排队和重试。
- wechat_linux 处理数据库、消息、媒体与原生提交；通用库不持有机器人配置或业务队列。
- wxbridge 处理实例、授权、Journal、发送确认、记忆和学习。模型线程返回结果，由主循环应用变更。
- helper 安装资产与源码分离，修改原生调用时成套核对 helper、agent 和 profile。
- 未知提交结果保留意图，不盲目重发或切换后端。
- 版本指纹、静态分析、合成测试和真实收发验证分别说明，不混称为已验证。

## 检查与发布

使用仓库虚拟环境，开发依赖见 requirements-test.txt。常用检查：python scripts/check-repository.py、git diff --check、git diff --cached --stat。

需要离线测试时使用 python -X utf8 scripts/test-offline.py。纯文档修改不增加业务测试；真实接口和微信收发只在明确授权的环境执行。CI 不访问生产服务器，不自动部署。

新增配置时更新默认值、examples 下的示例、对应校验和当前文档。提交前确认不含实例配置、密钥、账号信息、聊天、截图、数据库、登录状态和记忆，规则见 [安全与隐私](SECURITY.md)。
