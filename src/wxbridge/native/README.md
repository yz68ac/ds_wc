# wxbridge 本地发送接入

这里处理机器人应用层的配置、队列和本地 API。微信内部调用实现以 wechat_linux/native/ 为准。

| 文件 | 职责 |
| --- | --- |
| client.py | 连接现有 wxbridge 本地 API，提交文字、查询状态 |
| server.py | 主循环持有的非阻塞 Unix socket 服务 |
| protocol.py | 应用配置校验、任务状态映射，复用底层封包 |
| sender.py | 收件人白名单、dry-run 和应用层发送包装 |
| api.py | 旧应用入口适配 |
| helper.py / agent.js / profile.json | 保留的历史路径；底层资产以 wechat_linux/native 为准 |

正式 helper 独立安装在 root 管理的目录，经本地 socket 接收请求，再通过 Frida agent 调用指纹匹配的微信内部函数。修改项目源码不会自动更新安装资产。

提交返回之后，由账号主循环核对本人发出记录。结果未知时保留发送意图，不盲目重发。

[当前库接口](../../../docs/WECHAT_LINUX.md) · [原生发送研究](../../../docs/reference/NATIVE_ANALYSIS.md) · [历史本地 API](../../../docs/reference/NATIVE_API.md)
