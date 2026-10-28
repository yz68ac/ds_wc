# 配置

根 config.json 只配置账号共享资源；各群读取同级 instance/<id>/config.json，缺省业务字段来自 config.defaults.json。API 连接只在 provider 的 .env 中维护，参考 [provider 文档](../src/provider/README.md)。可通过 provider_env 指定绝对路径，或相对 config.json 的路径。

## 模型

model_clients 指定三个连接，例如 reply=deepseek、image=openai、background=deepseek。每个群分别创建客户端，即使连接名称相同也不共享实例。

models.reply/memory/avatar/participation 分别填写 model 和 options。模型 ID 没有试用默认值，启用任务缺失 ID 时报告 missing_model。模型必须属于对应通道连接；后台任务可以选择同一连接下的不同模型。options 中 max_tokens、timeout_seconds 控制预算；thinking 等按模型能力填写。没有自动切换模型。

旧 deepseek、model_profile、model_profiles、persona 字段，以及 queue.generation_concurrency 已移除，填写会报告旧配置错误。外部搜索通过 search.connection 选择 provider 连接。

外部搜索的 Search/Fetch 同时使用所选连接的密钥、接口地址和代理。例如 search.connection=deepseek 时读取 DEEPSEEK_PROXY_URL；显式代理仅用于该搜索客户端，不修改全局代理。未配置时保留 urllib 默认网络行为，代理失败不会自动切换为直连。

## 提示词与群身份

system_prompt_file 为群系统提示词文件，相对该实例目录解析；每次准备请求时读取。默认是中性助手说明，无固定人设、昵称、短答长度或标点要求。各群可以覆盖此文件。target.bot_name 和 proactive.opportunities.address_names 仅用于群称呼与候选识别，真实 @ 使用微信账号元数据。

记忆、关系、学习、头像分类及主动判断仍保留业务协议提示；memory.system_prompt_file 独立控制记忆整理。旧人格正文和本机临时模型试用覆盖已清除。

## 生命周期

同群三条模型通道独立串行，跨群并行。queue.max_pending 等仍控制业务接纳；没有账号级模型总并发额度。微信发送继续通过账号协调和确认队列。群 ID、队列文件、游标和已有业务数据不因本次迁移清空。

配置改变后重建群实例；同一路径下的系统提示词在下次请求准备时重读。

实例目录结构和启用方法见 [多群配置](MULTI_GROUP.md)，公开模板见 [实例模板](../examples/instance/README.md)。旧根 instances 列表与 overrides 不再加载。
