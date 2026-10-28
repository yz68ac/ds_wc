# model-provider

独立发行包名为 `model-provider`，Python 导入名为 `provider`，要求 Python 3.11 或以上。运行依赖为 OpenAI 和 Anthropic SDK。调用层不依赖微信、机器人配置、任务队列或提示词。

## 安装与构建

从仓库根目录单独安装：

```bash
python -m pip install ./src/provider
# 使用 uv 管理虚拟环境时：
uv pip install ./src/provider
```

安装后可在其他项目中使用 `from provider import create_client`，并执行 `python -m provider models` 或 `model-provider models`。SDK 依赖会随库自动安装。发行包尚未发布到公共包索引。

独立构建 wheel 和源码包：

```bash
uv build ./src/provider --out-dir ./dist/provider
# 将生成的 wheel 安装到另一环境：
python -m pip install ./dist/provider/model_provider-0.3.1-py3-none-any.whl
```

构建只收录 `provider` 及其适配器代码；真实 `.env`、`models.json` 和微信源码不进入 wheel 或源码包。常规安装使用使用方项目中的 `.env`，模型列表保存在该文件旁边的 `models.json`；也可用 `--env`、`--models-file` 指定路径。源码或 editable 安装继续使用 `src/provider/.env`，避免改变现有项目的配置位置。

直接从源码导入时，将 `src` 加入 `PYTHONPATH`；例如 Linux 使用 `export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"`。

协议转换位于 `adapters/`：`openai_chat_completions.py` 和 `openai_responses.py` 分别处理 OpenAI 两种接口，`anthropic_messages.py` 处理 Anthropic Messages，`deepseek.py` 在 Chat Completions 基础上处理 DeepSeek 特有参数。

本仓库使用 uv 时，执行 `uv sync` 会通过默认的 `provider` 依赖组以 editable 方式安装这个本地库及其 SDK 依赖。已有环境需要保留额外安装的包时使用 `uv sync --inexact`。独立微信 SDK wheel 的基础依赖仍为空。

## 配置与模型列表

在上述配置位置的 `.env` 中填写需要使用的 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 或 `DEEPSEEK_API_KEY`，并按需调整对应的 `BASE_URL`、`PROTOCOL` 和 `PROXY_URL`。实际 `.env` 不提交。支持 `KEY=value` 和单引号或双引号包围的值，不支持变量插值；进程环境变量优先，包括空值。读取配置不会修改进程环境变量。修改 `.env` 后需要重新创建客户端。

例如只接入 DeepSeek 时：

```dotenv
DEEPSEEK_API_KEY=填写你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_PROTOCOL=deepseek
```

Anthropic 默认使用已安装 SDK 的 API 版本和认证方式，无需配置 `ANTHROPIC_VERSION`、`ANTHROPIC_AUTH`。只有兼容旧版接口或特殊网关时才显式覆盖这两项；`ANTHROPIC_AUTH=api_key` 强制使用 `x-api-key`，`bearer` 使用 Bearer。SDK 2.x 的默认调用不发送旧 API 的采样参数。

安装后执行一次统一同步命令（从源码运行时先配置 `PYTHONPATH`）：

```bash
python -m provider models
```

命令会自动获取所有已填写 API Key 的服务商列表，未填写的自动跳过；成功结果写入同目录的 `models.json`。Anthropic 按 SDK 分页读取。某一家失败时保留其旧记录，其他成功结果仍会保存，同时命令返回非零退出码；全部未配置时报告 `no_configured_providers`。这个命令只请求模型列表，不调用生成接口。

使用其他配置文件可执行 `python -m provider --env /path/to/.env models`，结果默认写入该 `.env` 旁边的 `models.json`。也可以用 `--models-file /path/to/models.json` 指定保存路径。这两个参数放在 `models` 子命令前。

`models.json` 是自动维护的本地模型配置，包含文件版本号、各服务商的获取时间和模型 ID/显示名，以及用于匹配当前连接和账号的摘要；不保存 API Key、代理地址或接口地址，实际文件已被 Git 忽略。服务商对应的完整记录在成功获取后替换，文件通过临时文件原子写入。文件缺失时可重新同步；文件格式损坏时会报错并保留原文件。更换密钥、接口、协议或连接设置后，先重新同步；与新配置不匹配的旧列表不会用于路由。

当前目录格式为 schema 2，连接摘要字段名为 `source_fingerprint`。schema 1 的 `source` 仍可读取，下一次成功同步时自动保存为新版；名称调整不会清空旧列表。

程序内同步模型列表时，调用方先创建各连接的客户端，将它们交给路由器使用，最后统一关闭。`ModelRouter` 只解析模型别名、读取和更新本地列表；不会创建或关闭传入的客户端。未传入所需客户端时，请求报告 `client_not_configured`。

## 客户端生命周期

`create_client("deepseek")` 读取对应连接配置，创建一个统一 `Client`，内部持有一个 SDK 实例。调用方可以重复调用生成和模型列表接口，并负责最终 `close()`：

```python
from provider import ModelRequest, create_client

client = create_client('deepseek')
try:
    models = client.fetch_models()  # 显式访问服务端模型列表
    if not models:
        raise RuntimeError('服务端没有返回模型')
    result = client.generate(ModelRequest(
        model=models[0].id,  # 单连接客户端使用真实模型 ID
        messages=[{'role': 'user', 'content': '你好'}],
        options={'timeout_seconds': 30},
    ))
    print(result.message['content'])
finally:
    client.close()
```

也可以使用 `with create_client('deepseek') as client:`，作用域结束由调用方的上下文关闭。一个客户端绑定一个连接和协议，可以请求该连接下的多个模型；请求结束只释放响应，不关闭 SDK。每次请求独立传入超时、模型和生成参数，不修改共享 SDK 的设置。

这一层不提供线程池、客户端缓存、并发计数或关闭等待。调用方负责并发调度，在所有请求结束后关闭客户端；跨进程时应在各进程里分别创建实例。`close()` 可以重复调用，关闭后发起请求会报告 `client_closed`。

正式类名为 `Client`、`ModelRouter`；旧的 `ModelClient`、`ProviderClient` 保留为兼容别名。获取远端模型使用 `Client.fetch_models()`，读取已加载的本地模型使用 `ModelRouter.list_cached_models()`；旧的 `list_models()` 也保留为兼容别名。

协议适配器使用 `build_payload()` 和 `parse_response()`，图片请求与响应分别由 `build_image_payload()`、`parse_image_response()` 处理。适配器不再提供旧 HTTP 实现的 `endpoint()` / `headers()`；请求地址和认证交给 SDK。原始字节与文本的 UTF-8 编码/解码仍使用标准的 `encode()` / `decode()`。

## OpenAI 图片生成

`client.generate_images(ImageRequest(...))` 使用当前客户端的 OpenAI SDK 请求 `/images/generations`。OpenAI 的 Chat Completions、Responses 配置和 `openai_compatible` 连接均可调用；服务端须提供该接口。DeepSeek、Anthropic 连接会报告 `unsupported_image_generation`。

```python
from pathlib import Path
from provider import ImageRequest, create_client

with create_client('openai') as client:
    result = client.generate_images(ImageRequest(
        model='gpt-image-2.5-flare',  # 显式指定账号可用的图片模型
        prompt='一只坐在窗边晒太阳的橘猫，水彩画风格',
        options={
            'size': '1024x1024',
            'quality': 'low',
            'n': 1,
            'output_format': 'png',
            'timeout_seconds': 300,
        },
    ))

# 保存位置由使用方决定，库本身不会写图片文件。
image = result.images[0]
if image.data is not None:
    Path('cat.png').write_bytes(image.data)
```

`ImageResult.images` 包含 `GeneratedImage`；`data` 是解码后的图片字节，`url` 和 `revised_prompt` 则按服务端返回保留。库不会下载返回的 URL。结果还包含创建时间、用量、输出格式、尺寸、质量和背景（服务端未返回时为空）；用量沿用 `prompt_tokens`、`completion_tokens`、`total_tokens`，并保留文本/图片 token 明细。提示词、图片字节、URL 和改写提示词均不会出现在请求/结果的默认 repr 中。

支持的请求选项：

| 选项 | 含义 |
| --- | --- |
| `n` | 数量，1–10，实际限制由模型决定 |
| `size` | 默认 `1024x1024`；可传 `auto` 或 `宽x高` 字符串，具体分辨率约束由模型验证 |
| `quality` | 默认 `low`；可传 `auto/low/medium/high/xhigh/max`，兼容服务也可传 `standard/hd` |
| `output_format` | `png/jpeg/webp` |
| `background` | `auto/opaque/transparent`；透明背景不能使用 JPEG |
| `output_compression` | 0–100，需同时指定 `jpeg` 或 `webp` |
| `moderation` | `auto/low` |
| `safety_identifier` | 使用方提供的安全标识 |
| `timeout_seconds` | 本次请求超时，默认 300 秒，支持 1–300 秒 |

兼容服务可按需传入 SDK 的旧字段 `response_format`、`style`、`user`；GPT Image 模型通常直接返回 base64，不要设置旧的 `response_format` 或 `style`。SDK 的服务端参数、模型支持情况与账号权限以 [OpenAI Docs 图片生成接口](https://developers.openai.com/api/reference/python/resources/images/methods/generate) 为准。图片接口显式使用非流式响应；编辑和流式图片输出不在此接口中。

也可用 `router.generate_images(ImageRequest(model='openai/图片模型ID', ...))` 按本地模型列表路由到调用方传入的客户端。`deadline` 与文本接口一样接受单调时钟绝对截止时间；已过期时不会发起请求。SDK 自动重试保持关闭，客户端由调用方关闭。文本和图片响应均由 SDK 正常读取、解析，再转换为统一结果；本库不额外设置响应体字节上限。

## 模型别名路由

使用本地 `models.json` 和跨服务商模型别名时，将调用方创建的客户端传入 `ModelRouter`：

```python
from provider import ModelRequest, ModelRouter, create_client, load_config

config = load_config()
with create_client('deepseek', config=config) as deepseek:
    router = ModelRouter.from_env(clients={'deepseek': deepseek})
    models = router.list_cached_models('deepseek')  # 只读本地列表，不联网
    if not models:
        raise RuntimeError('请先运行 python -m provider models')
    result = router.generate(ModelRequest(
        model=models[0].alias,  # deepseek/模型ID
        messages=[{'role': 'user', 'content': '你好'}],
    ))
```

多个服务商通过 `clients={'openai': openai_client, 'anthropic': anthropic_client, 'deepseek': deepseek_client}` 注册；调用方创建和关闭这些对象。路由器会检查客户端的连接、协议及凭据配置，避免将请求发到不匹配的连接。`sync_models()` 使用已注册的客户端更新已配置服务商的列表，`refresh_models('deepseek')` 单独更新一家；这两种操作会保存成功结果。目录更新的调度和互斥由调用方负责。

同名模型请使用 `openai/模型ID`、`anthropic/模型ID` 或 `deepseek/模型ID`；只在模型名唯一时可直接使用原始 ID。列表表示接口返回的模型，并不保证每个模型支持文本生成、工具、图片或当前选定的协议。

这套配置与发现接口属于独立 `provider`；现有微信桥接仍使用仓库根目录的 `.env` 和原来的模型 profile，不会自动切换或读取此文件。

## 手动注册

也可以使用 `ModelRegistry(models, connections)` 或 `ModelRegistry.from_profiles(profiles)` 手动注册模型，将它与调用方的 `clients` 一起传入 `ModelRouter(registry, clients=clients)`。客户端字典的键对应 `ModelSpec.connection`。`Client(settings, api_key=key)` 可用于已有配置格式；`ModelClient` 是 `Client` 的兼容别名。所有这些入口均由调用方负责关闭创建的客户端。

`ModelRequest` 支持消息、工具、工具选择、原生搜索及每次调用的 `options`。`generate()` 返回包含消息、结束原因和用量的 `ModelResult`。工具调用后，单连接使用 `client.assistant_message(result.message)`，路由器使用 `router.assistant_message(model, result.message)` 生成续接消息，再与工具结果一起提交；续接状态绑定原模型和连接。

`stream()` 目前尚未实现，会抛出 `ModelError('unsupported_stream')`。调用层不自动重试；错误对象的 `code`、`status` 和 `retryable` 可供调用方制定策略。OpenAI、DeepSeek 和兼容 Chat Completions 使用 OpenAI SDK；Anthropic 使用 Anthropic SDK。独立发行包的运行依赖声明在本目录的 `pyproject.toml`。
