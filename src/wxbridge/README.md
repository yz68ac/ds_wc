# wxbridge 代码地图

wxbridge 组合 provider 和 wechat_linux，负责账号资源、群聊与私聊实例、模型任务、业务队列与发送确认。

| 入口 | 职责 |
| --- | --- |
| [wxbridge_cli](../wxbridge_cli) | 安装、诊断、部署和交互命令 |
| [config](config) / [instances.py](instances.py) | 只读配置、文档转换、独立会话实例发现 |
| [app/runtime.py](app/runtime.py) | 运行策略与账号资源锁 |
| [app/sessions/coordinator.py](app/sessions/coordinator.py) / [account.py](app/sessions/account.py) | 多实例调度、共享 reader、发送闸门 |
| [app/sessions/conversation.py](app/sessions/conversation.py) | ConversationSession：群聊和私聊实例的生命周期及资源所有权 |
| [app/execution/model_runtime.py](app/execution/model_runtime.py) | reply/image/background 串行通道，client 延迟创建 |
| [app/tasks](app/tasks) | 回复、参与判断、记忆、学习与图片任务的调度 |
| [app/delivery](app/delivery) | 发送意图、提交和真实发出记录确认 |
| [host/login.py](host/login.py) / [host](host) | 登录运行策略、进程监督及部署环境；scripts 仅保留登录命令入口 |
| [ai](ai) / [contracts/image_jobs.py](contracts/image_jobs.py) | 模型输入输出与工具；图片提案及排队任务的交接类型 |
| [storage/image_artifacts.py](storage/image_artifacts.py) / [image_context.py](storage/image_context.py) | 图片文件及校验 / 已确认图片的上下文索引 |
| [storage](storage) | Journal、记忆、关系和学习数据；仅在存储边界转换任务 JSON |
| [wechat](wechat) / [native](native) | 业务显示与微信适配 / helper 发送策略 |
| [submission_api](submission_api) | 独立启停的本地任务提交和状态查询 API |
| [web](../web) | Web 后端与前端；实例、提示词编辑器和认证连接分别管理状态 |

实例配置位于 `instance/<id>/config.json`。账号资源共享只读，队列和状态按实例隔离；模型线程不直接修改 Journal 或微信数据库。

`helper_transport.enabled` 控制 helper 发送，`submission_api.enabled` 控制本地提交 API。旧 `native_api` 文档在读取时转换，保留旧开关同时控制发送和提交的行为；与新字段混写会被拒绝。新安装默认只启用 helper，提交 API 按需开启。

[模型调用用法](../../docs/PROVIDER.md) · [微信适配状态](../../docs/WECHAT_LINUX.md)
