# 多群运行

instances 中每项指定唯一 id、稳定 chat 群 ID 和 enabled。每个启用群创建一个 GroupSession，拥有自己的 Journal、上下文、记忆和三条模型通道。

overrides 支持 system_prompt_file、model_clients、models，以及 target、queue、memory、search、vision、proactive 等业务设置。连接与密钥统一在 provider 配置中维护。队列路径和群身份由实例编译器生成，不能用 overrides 改写。

每群 reply、image、background 各一个 FIFO 工作线程及一个复用 client；没有群间共享 client 或模型总额度。账号协调器只统一消息读取、微信发送门禁及发送确认。后台任务不再因另一群前台请求而被强制终止。

未确认的微信发送仍阻止再次提交，停用群的队列历史仍参与核对。模型线程不访问 Journal 或微信数据库。关闭时等待在途请求后释放 client。

配置示例参见 [CONFIG](CONFIG.md) 和仓库 config.example.jsonc。
