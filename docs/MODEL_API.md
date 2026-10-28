# 群模型运行时

每个 GroupSession 拥有 GroupModels，reply、image、background 三条通道各自持有一个延迟创建的 provider Client、FIFO 队列和工作线程。同通道串行，群间和通道间并行，没有账号模型总额度。发送协调与微信确认仍由账号层串行管理。

连接、API Key、代理及协议读取 provider 的 .env；可用 provider_env 指定文件。进程环境变量优先。wxbridge 不再加载根目录 .env 中的模型密钥。已有 provider 配置不会被覆盖。

model_clients.reply/image/background 填 provider 连接名。models.reply/memory/avatar/participation 各包含 model 和 options；model 必须显式填写，options 包含 max_tokens、timeout_seconds、thinking 等参数。后台三个角色使用同一个连接，可选择不同模型。模型目录通过 python -m provider models 单独更新，启动不调用模型 API。

GroupSession.submit_images(ImageRequest(...), deadline=None) 返回 Future[ImageResult]，请求必须明确指定模型。默认 low、1024x1024；库不保存图片，也不发送微信图片。调用方从主循环收集结果，避免在 Future 回调中修改群状态。

任务数据在提交时复制。线程通过结果/事件队列返回，数据库和记忆由主循环提交。工具循环占用同一个回复任务；头像分类直接复用后台 client。参与判断完成后由主循环提交回复任务。

取消排队任务不会请求 API；运行中取消只废弃结果，实际请求结束前通道不启动下一项。任务等待计入截止时间，出队和应用前分别验证有效性。关闭停止接纳、取消等待项、等待活动请求结束，最后关闭 client；SDK 自动重试关闭。微信 UI 与图片准备继续使用隔离进程。

旧 profile、DeepSeekClient/ModelClient 入口、persona 兼容配置和账号模型并发额度已移除。不得把旧连接字段放回 wxbridge 配置。

各群的 model_clients 与 models 保存在 instance/<id>/config.json；账号文件不再放模型选择。
