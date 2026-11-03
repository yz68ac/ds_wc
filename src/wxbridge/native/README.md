# wxbridge helper 发送适配

这里处理调用微信 helper 时的发送策略，微信内部调用实现由 wechat_linux/native 提供。

| 文件 | 职责 |
| --- | --- |
| [settings.py](settings.py) | 独立校验 helper_transport 配置、授权名单及发送大小 |
| [sender.py](sender.py) | dry-run、目标授权和文本 / 图片提交，图片文件由共享存储模块读取 |

对外的本地任务提交、请求去重和状态查询位于 [submission_api](../submission_api)，发送确认业务位于 [app/delivery](../app/delivery)。启用 helper 不会自动开启提交 API。
