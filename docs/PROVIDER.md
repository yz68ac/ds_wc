# provider 用法

更新：2026-10-29。对应 `model-provider 0.3.1`，以仓库当前实现为准。

`provider` 是独立的同步 Python 模型调用库，要求 Python 3.11+。它负责连接配置、协议适配、SDK 请求和统一结果；调用方负责 client 生命周期、任务队列、并发、重试与工具执行。

## 1. 安装与配置

从仓库根目录安装：

```bash
python -m pip install ./src/provider
# 开发时使用 editable 安装：
python -m pip install -e ./src/provider
```

发行包名是 `model-provider`，导入名是 `provider`。安装会带上 OpenAI 和 Anthropic SDK。直接使用源码时，将仓库的 `src` 加入 `PYTHONPATH`。

配置位置：

| 安装方式 | 默认读取 |
| --- | --- |
| 仓库源码 / editable 安装 | `src/provider/.env` |
| 普通 wheel 安装 | 当前工作目录的 `.env` |
| 显式指定 | `load_config("/path/to/.env")` |

按需创建 `.env`，只填写要使用的连接：

```dotenv
OPENAI_API_KEY=填写密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_PROTOCOL=openai_responses
OPENAI_PROXY_URL=

ANTHROPIC_API_KEY=填写密钥
ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
ANTHROPIC_PROTOCOL=anthropic_messages
ANTHROPIC_PROXY_URL=http://127.0.0.1:10808

DEEPSEEK_API_KEY=填写密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_PROTOCOL=deepseek
DEEPSEEK_PROXY_URL=
```

这里的代理地址只是本机代理示例，使用前确认对应服务已存在。非空 `*_PROXY_URL` 会显式交给 SDK；留空时沿用 SDK 的默认网络环境。需要保证某个连接直连时，还应清理进程继承的 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` 及其小写变量。

进程环境变量优先于文件，包括空值。读取配置不修改进程环境；文件仅支持 `KEY=value` 和成对引号，不做变量插值。修改配置后重新创建 client。密钥、真实 `.env` 和 `models.json` 不提交到 Git。

Anthropic 的版本和认证默认交给 SDK；特殊网关才需要 `ANTHROPIC_VERSION` 或 `ANTHROPIC_AUTH=api_key/bearer`。

## 2. 支持的调用方式

| 连接 / 协议 | 使用的 SDK 和接口 | 当前封装 |
| --- | --- | --- |
| `openai / openai_responses` | OpenAI SDK `responses.create` | 文本、图片输入、函数工具、原生搜索 |
| `openai / openai` | OpenAI SDK `chat.completions.create` | 文本、图片输入、函数工具 |
| `anthropic / anthropic_messages` | Anthropic SDK `messages.create` | 文本、图片输入、函数工具、原生搜索、思考参数 |
| `deepseek / deepseek` | OpenAI SDK `chat.completions.create` | 文本、函数工具、DeepSeek 思考参数 |
| OpenAI Images | OpenAI SDK `images.generate` | 文生图，默认 `low / 1024x1024` |

实际模型能力由服务端决定；兼容地址不保证支持表中所有功能。`.env` 当前固定使用 `openai/anthropic/deepseek` 三个连接名，协议按各自允许的值配置。低层 `Client` / `ModelRegistry` 还可用于程序内显式连接配置。

当前没有异步 client、流式生成、图片编辑、音频、视频、Realtime 或 HTTP 网关。`Client.stream()` 和 `ModelRouter.stream()` 返回 `unsupported_stream`。官方 SDK 提供的其他接口不会自动成为本库的公共接口。

OpenAI 上游参考：[Responses](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Images generate](https://developers.openai.com/api/reference/python/resources/images/methods/generate)。本库只封装上表列出的部分能力，图片生成当前使用独立的 Images 接口。

## 3. 单连接调用：推荐入口

```python
from provider import ModelRequest, create_client, load_config

config = load_config("/path/to/.env")
with create_client("deepseek", config=config) as client:
    result = client.generate(
        ModelRequest(
            model="deepseek-flash",  # 使用自己账号实际可用的模型 ID
            messages=[{"role": "user", "content": "你好"}],
            options={
                "thinking": "enabled",
                "reasoning_effort": "max",
                "max_tokens": 8192,
                "timeout_seconds": 120,
            },
        ),
        require_complete=True,
    )
    print(result.message.get("content", ""))
    print(result.finish_reason, result.usage)
```

`create_client()` 创建一个绑定连接的 client，内部持有一个官方 SDK 实例。此后复用它，在每个 `ModelRequest` 中传模型和参数；请求不会修改共享 client。单连接调用不依赖本地模型目录，`model` 直接填服务端模型 ID。

调用方也可以使用 `try/finally: client.close()`。关闭幂等；关闭前应由调用方等待在途请求结束。库不管理并发线程池、全局 client 缓存或并发上限。

### 文本请求选项

| 字段 | 当前校验及含义 |
| --- | --- |
| `max_tokens` | 整数 1–128000；上游可能有更小限制，思考请求需留出预算 |
| `timeout_seconds` | 1–300 秒，未设置时文本调用通常采用 60 秒 |
| `temperature` | `None` 或 0–2；是否发送取决于协议适配器 |
| `thinking` | `enabled/disabled`，默认关闭 |
| `reasoning_effort` | `none/low/medium/high/xhigh/max` 或 `None`；实际可用档位取决于协议和模型 |
| `thinking_mode` | Anthropic 使用 `adaptive/manual`；关闭思考时还支持 `between_tools` |
| `thinking_budget_tokens` | Anthropic 手动思考预算，至少 1024 且小于 `max_tokens` |

例如 Sonnet 可在请求中设置：

```python
request = ModelRequest(
    model="claude-sonnet-5-5",
    messages=[{"role": "user", "content": "你好"}],
    options={
        "thinking": "enabled",
        "thinking_mode": "adaptive",
        "reasoning_effort": "medium",
        "max_tokens": 8192,
        "timeout_seconds": 120,
    },
)
# 交给 create_client("anthropic", config=config) 创建的 client.generate(request)
```

`deadline` 可传 `time.monotonic() + 秒数` 的绝对截止时间。它与请求超时共同约束本次调用，不是 Unix 时间戳。

## 4. 模型列表与按模型路由

统一同步所有已填写密钥的服务商：

```bash
python -m provider models
python -m provider --env /path/to/.env --models-file /path/to/models.json models
```

`--env`、`--models-file` 放在 `models` 前。命令只访问模型列表接口，默认把结果写到 `.env` 同目录的 `models.json`。未配置密钥的服务商跳过；某家失败则保留旧记录，其他成功结果仍保存，退出码为非零。目录记录包含模型 ID、显示名、时间和连接指纹，不保存密钥。

程序内获取列表和路由：

```python
from contextlib import ExitStack
from provider import ModelRequest, ModelRouter, create_client, load_config

config = load_config("/path/to/.env")
with ExitStack() as stack:
    clients = {
        name: stack.enter_context(create_client(name, config=config))
        for name in config.connections if config.api_key(name)
    }
    router = ModelRouter.from_env("/path/to/.env", clients=clients)
    sync = router.sync_models()  # 显式联网并更新本地目录
    if sync.errors:
        print(sync.errors)
    print(router.list_cached_models())  # 只读取已加载的目录

    result = router.generate(ModelRequest(
        model="deepseek/deepseek-flash",
        messages=[{"role": "user", "content": "你好"}],
        options={"max_tokens": 1200},
    ))
```

`ModelRouter` 不创建或关闭传入的 clients。推荐使用 `服务商/模型ID` 别名；短模型 ID 重名时会报告歧义。更换密钥、地址、协议或连接配置后重新同步，旧连接指纹对应的目录不会参与新连接路由。

## 5. 工具、图片输入与搜索

`ModelRequest.messages` 使用 `role/content` 消息结构。`tools/tool_choice` 使用函数工具描述，返回的工具调用在 `result.message["tool_calls"]` 中。工具由调用方执行，再将结果作为 `role="tool"` 消息传回。

把 assistant 消息加入后续请求时使用 `client.assistant_message(result.message)`；通过 router 调用时使用 `router.assistant_message(model_alias, result.message)`。这会保留协议所需的思考签名和工具续接信息；续接状态不能跨连接或模型复用。

图片输入通过消息中的 `image_url` 内容块交给适配器，格式和来源需满足适配器限制。`native_search` 用于 Responses / Anthropic 的原生搜索；Chat Completions 和 DeepSeek 适配器不提供原生搜索。wxbridge 的外部搜索、联网抓取和工具循环属于上层业务。

## 6. OpenAI 文生图

```python
from pathlib import Path
from provider import ImageRequest, create_client, load_config

config = load_config("/path/to/.env")
with create_client("openai", config=config) as client:
    result = client.generate_images(ImageRequest(
        model="YOUR_IMAGE_MODEL_ID",  # 替换为账号可用的图片模型
        prompt="窗边晒太阳的橘猫，水彩画",
        options={"output_format": "png"},
    ))

image = result.images[0]
if image.data is not None:
    Path("cat.png").write_bytes(image.data)
elif image.url:
    print("服务端返回了 URL，下载由调用方决定")
```

默认 `quality=low`、`size=1024x1024`、超时 300 秒。可配置 `n/size/quality/background/output_format/output_compression/moderation/safety_identifier` 等选项；模型支持范围以服务端为准。

`ImageResult.images` 中的 `GeneratedImage.data` 是已做 Base64 解码的字节，也可能只有 `url`；库不写文件、不自动下载 URL。Anthropic 和 DeepSeek 连接不支持这个接口。

## 7. 公共接口与错误处理

| 接口 | 用途 |
| --- | --- |
| `load_config(path=None)` | 读取连接及凭据 |
| `create_client(connection, config=...)` | 创建调用方持有的 client |
| `Client.generate(ModelRequest, ...)` | 指定模型的文本、多模态和工具请求 |
| `Client.generate_images(ImageRequest, ...)` | OpenAI 文生图 |
| `Client.fetch_models(timeout=30)` | 获取远端模型列表 |
| `Client.assistant_message(message)` | 构造协议兼容的 assistant 续接消息 |
| `Client.close()` / 上下文管理器 | 关闭 SDK 连接 |
| `ModelRouter.from_env(..., clients=...)` | 从目录建立路由 |
| `sync_models/refresh_models/list_cached_models` | 同步全部、刷新单家、读取本地列表 |
| `capture_metrics()` | 收集当前上下文中的请求耗时、用量和错误码 |

`ModelResult` 提供 `message/finish_reason/usage/search`。统一异常 `ModelError` 提供 `code/retryable/status`，例如 `missing_api_key/sdk_not_installed/client_closed/http_401/http_429/connection_failed/output_limit/unsupported_stream`。SDK 内部重试关闭，重试策略由调用方管理。

旧 `ModelClient/ProviderClient/list_models` 别名及绑定模型的 `request/complete/chat` 入口暂时保留；新代码优先采用本文的 `Client.generate` 与 `ModelRouter`，避免依赖隐式模型设置。
