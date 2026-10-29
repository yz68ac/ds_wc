# wxbridge 代码地图

wxbridge 组合 provider 和 wechat_linux，负责账号级读库、群/私聊实例、参与判断、生成、业务队列与发送确认。

| 入口 | 职责 |
| --- | --- |
| cli.py / config.py / instances.py | CLI、账号配置、instance 目录发现与有效配置 |
| app/runtime.py | 运行策略和账号资源锁 |
| app/coordinator.py / account.py | 多实例调度、共享 reader、发送闸门 |
| app/bot.py | GroupSession：身份、队列、模型通道和业务控制器 |
| app/model_runtime.py | reply/image/background 三条串行通道，client 延迟创建 |
| app/proactive.py | 参与判断、前台回复移交和参与窗口 |
| app/delivery.py / confirmation.py | 发送意图、提交及本人发出记录确认 |
| ai/ | 模型输入输出、搜索、图片、记忆协议与语义资料 |
| storage/ | Journal、人物记忆、关系与学习数据 |
| wechat/ | 机器人业务的显示、消息与读库适配 |
| native/ | 本地 API、应用配置与底层发送适配 |

实例配置位于 `instance/<id>/config.json`，enabled 控制启用，chat 为群或用户稳定 ID。队列和状态按实例分目录。账号负责共享微信读库和有序发送；模型线程不直接修改 Journal 或微信数据库。

[模型调用用法](../../docs/PROVIDER.md) · [微信适配状态](../../docs/WECHAT_LINUX.md) · [历史参考](../../docs/reference/README.md)
