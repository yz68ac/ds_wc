# model-provider

独立同步模型调用库，Python 导入名 provider，版本 0.3.1，要求 Python 3.11+。依赖 OpenAI 和 Anthropic SDK，不依赖微信或 wxbridge。

从仓库根目录安装：python -m pip install ./src/provider。

主要入口：

| 接口 | 用途 |
| --- | --- |
| create_client(connection, config=...) | 创建并复用一个连接的 SDK client |
| Client.generate(ModelRequest(...)) | 指定模型、消息和请求参数 |
| Client.generate_images(ImageRequest(...)) | OpenAI 文生图，默认 low / 1024x1024 |
| Client.fetch_models() | 获取远端模型列表 |
| ModelRouter | 使用本地目录和显式传入的 clients 按模型路由 |
| Client.close() / with | 调用方关闭 client |

源码/editable 安装读取 src/provider/.env；普通 wheel 安装读取调用方当前目录的 .env；load_config(path) 可显式指定。统一同步模型列表使用 python -m provider models。

调用方负责并发、排队、工具执行、重试，以及等待请求结束后关闭资源。流式生成尚未实现。

配置、代理、完整示例、路由、工具续接和错误处理统一维护在 [provider 用法](../../docs/PROVIDER.md)。

独立构建：uv build ./src/provider --out-dir ./dist/provider。真实 .env、models.json 和微信源码不进入发行包。
