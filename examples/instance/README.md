# 群实例模板

将本目录的 config.example.jsonc 内容填写成严格 JSON（去掉注释），保存到项目 instance/<实例ID>/config.json，并复制 system.md 到同一目录。实例 ID 使用英文字母、数字、下划线或连字符，最长48字符，首字符须为字母或数字。

chat 填稳定的 xxx@chatroom ID；enabled=true 才启动。配置可以省略使用默认值的字段。至少为回复和已启用的后台任务填写模型 ID；连接、密钥、代理仍由 provider 的 .env 管理。

运行发送前还须把群 ID 加入账号配置 native_api.allowed_chat_ids。system_prompt_file 相对实例目录解析；记忆和队列仍存于运行目录，不存入此目录。

实际 instance/ 目录不会提交 Git。根 config.json 仅放账号配置，参考 examples/account.config.example.jsonc。
