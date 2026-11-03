# 会话实例模板

将本目录的 config.example.jsonc 内容填写成严格 JSON（去掉注释），保存到项目 instance/<实例ID>/config.json，并复制 system.md 到同一目录。实例 ID 使用英文字母、数字、下划线或连字符，最长48字符，首字符须为字母或数字。

chat 填稳定的群聊 xxx@chatroom 或私聊 wxid_example ID；enabled=true 才启动。配置可以省略使用默认值的字段。至少为回复和已启用的后台任务填写模型 ID；群聊开启参与判断时还需参与模型，私聊跳过参与判断。连接、密钥、代理仍由 provider 的 .env 管理。

运行发送前还须把聊天 ID 加入账号配置 helper_transport.allowed_chat_ids；管理台“绑定账号与实例会话”可完成授权。system_prompt_file 相对实例目录解析；记忆和队列仍存于运行目录，不存入此目录。

vision.enabled 默认开启，负责理解明确引用的图片；vision.context_images_enabled 默认关闭，开启后额外附带当前上下文中的图片。二者要求回复模型支持图片输入，每次最多 4 张，与引用图片共用限制。

image_generation.enabled 控制文生图和引用改图，需配置 model_clients.image 与 models.image，并让回复模型支持工具调用。改图时在微信中引用图片并描述修改要求；必须实际读到该引用原图，普通历史图片不自动作为改图来源。OpenAI 与 Gemini 参数及调用示例见 [Provider 用法](../../docs/PROVIDER.md)。

实际 instance/ 目录不会提交 Git。根 config.json 仅放账号配置，参考 examples/account.config.example.jsonc。
