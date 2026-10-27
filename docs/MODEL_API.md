# 模型供应商适配

业务统一使用 `ModelClient`、`ModelError`；供应商负责请求字段、HTTP 地址与认证、响应解析和工具续接。已实现 DeepSeek、Chat Completions 兼容、OpenAI Responses、Anthropic Messages。微信、人格、记忆来源校验、队列和并发调度保持原分工。

没有新增常驻服务、第三方 SDK 或动态插件系统。代码仍在同一个仓库内，不单独发布模型包。本轮只修改本地源码，未更新 Linux 线上服务。

## 1. 文件分工

```text
wxbridge/ai/
  client.py                    统一调用、角色设置、HTTP、截止时间、用量和错误
  roles.py                     reply / participation / memory / avatar 配置继承
  providers/
    base.py                    ModelRequest / ModelResult / ModelError / ProviderAdapter
    chat_completions.py        通用 Chat Completions 请求与结果转换
    deepseek.py                DeepSeek thinking 参数
    responses.py               OpenAI Responses、加密思考续接、原生搜索
    anthropic.py               Messages、签名思考续接、原生搜索
    common.py                  图片、工具、引用与安全转换
    __init__.py                显式供应商注册表
  search.py                    工具执行、搜索预算和来源处理
  native_search.py             供应商原生联网与有界 pause_turn 续接
```

适配器不读 `.env`、不查数据库、不操作微信、不管理任务，也不自行重试。密钥由统一客户端按照角色配置读取后传入认证方法；适配器本身可在不加载 bot 配置或微信库的情况下导入。

## 2. 业务接口

```python
from wxbridge.ai.client import ModelClient, ModelError

client = ModelClient(cfg, role="reply")
try:
    text = client.chat(messages)
except ModelError as exc:
    # 错误码与重试标记交给原任务队列；不要在这里重复发送微信消息。
    handle_failure(exc.code, exc.retryable)
```

`cfg` 是已加载的 bot 配置；示例中的 `messages`、`handle_failure` 由业务提供。

| 方法 | 返回与用途 |
| --- | --- |
| `chat(messages)` | 纯正文，检查截断、过滤和空回复 |
| `complete(messages, tools=None, tool_choice=None, deadline=None, require_complete=False)` | 助手消息字典，包含正文及工具调用；结构化任务通常设置 `require_complete=True` |
| `request(messages, ...)` | `ModelResult(message, finish_reason, usage)`，保留结束原因与用量 |
| `assistant_message(message)` | 生成工具循环的助手续接消息，由对应适配器保留必要的隐藏状态 |

`deadline` 是本进程 `time.monotonic()` 的绝对截止时间。多轮搜索继续共用总截止时间；模型接口不会内部自动重试或自动切换供应商。

`DeepSeekClient`、`DeepSeekError` 保留为同一实现的兼容别名。旧诊断脚本无需立刻改名，新业务入口已经使用中性名称。

## 3. 通过命名连接选择供应商

默认配置已内置三套连接：[config.defaults.json](../config.defaults.json)。完整带注释示例在 [config.example.jsonc](../config.example.jsonc)，另保留 [切换示例](../config.providers.example.jsonc)。实际启动配置仍为 `config.json`，不能把 JSONC 文件直接传给现有配置加载器。

无需再手工合并连接。直接在实际 `config.json` 中选择：

```json
"model_profile": "openai_terra"
```

内置 `deepseek_chat`、`openai_terra`、`claude_messages` 三个连接。默认 `model_profile=null` 沿用原来的 DeepSeek/记忆配置，不切换接口；各角色的 `profile=null` 表示继承全局选择。未选中的连接不要求提供密钥。对应密钥环境变量分别为 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，只写项目根目录 `.env`，不要写进 JSON 或 Git。

本地新建的 `config.json` 已展开连接和各角色选择项，便于直接查看和修改；这是本地模型覆盖配置，Linux 的群、路径、密钥和运行配置没有更新。连接地址或模型需要自定义时，可直接编辑其中的 `model_profiles`；未覆盖的字段继续继承默认配置。

若希望主回复用 Terra、记忆继续用 DeepSeek：

```json
{
  "model_profile": "openai_terra",
  "models": {
    "memory": { "profile": "deepseek_chat" }
  }
}
```

继承顺序：旧 `deepseek/memory` 和角色默认值 → 当前角色选择的命名连接 → 当前角色的显式覆盖。角色 `profile` 替代全局选择；已有角色中的 `provider/base_url/model` 等显式字段仍然优先，合并时应检查这些旧值。主回复、参与判断、记忆、头像都支持独立选择。默认示例仍仅关闭参与判断的思考。

| provider | 接口后缀 | 认证 | 原生搜索 |
| --- | --- | --- | --- |
| `deepseek` | `/chat/completions` | Bearer | 不支持，使用外部搜索 |
| `openai` / `openai_compatible` | `/chat/completions` | Bearer | 不支持，使用外部搜索 |
| `openai_responses` | `/responses` | Bearer | 支持 `web_search` |
| `anthropic` / `anthropic_messages` | `/messages` | `x-api-key`，兼容网关可选 Bearer | 支持版本化 `web_search` |

`base_url` 是接口前缀，例如 OpenAI 为 `https://api.openai.com/v1`、Anthropic 为 `https://api.anthropic.com/v1`。适配器追加上表后缀；不要把完整 `/responses` 或 `/messages` 再填入前缀。兼容网关按它实际提供的前缀填写。

协议支持不代表每个模型或网关都支持所有能力。若网关只兼容消息和函数调用、没有内置搜索，在该连接中配置 `"native_search_supported": false`；`auto` 就会预选外部搜索。此字段不能把不支持原生搜索的协议变成支持。

## 4. 保留的角色覆盖配置

现有配置格式继续有效：`deepseek` 是历史默认连接，`models` 是四个角色的覆盖值。人格提示词仍沿用现有文件，不属于供应商适配器。

例如只替换主回复，在实际 `config.json` 中合并以下片段：

```json
{
  "models": {
    "reply": {
      "provider": "openai_compatible",
      "base_url": "https://gateway.example/v1",
      "api_key_env": "OTHER_MODEL_API_KEY",
      "model": "your-model-name",
      "thinking": "disabled",
      "reasoning_effort": null,
      "temperature": 1.0,
      "timeout_seconds": 60,
      "max_tokens": 1200
    }
  }
}
```

这是示例地址和模型名。密钥放入项目 `.env` 中的 `OTHER_MODEL_API_KEY`，不要写入 JSON 或 Git。修改连接配置后重启监听生效。

`base_url` 填到 `/chat/completions` 之前：上例实际请求 `https://gateway.example/v1/chat/completions`。`provider=openai` 是保留的旧拼写，与 `openai_compatible` 使用相同适配器；`provider=deepseek` 在共同请求上增加 DeepSeek 专有 `thinking` 字段。

这里的兼容指当前请求格式，不能保证所有模型都支持图片、工具、`temperature`、`max_tokens` 或相同思考档位。通用适配器沿用现有参数映射，没有伪造自动能力探测；服务或模型不接受字段时返回安全错误，不悄悄切换模型。示例关闭思考并省略深度字段，启用前应确认目标模型实际约定。

`reply` 不改变 `participation`、`memory` 或 `avatar`。需要替换其它角色时，在对应覆盖对象设置连接、模型及思考参数；特别注意记忆和头像原本还继承 `memory` 的模型默认值，不能只换主回复就假定全部请求都已迁移。当前参与判断关闭思考、其它角色的原设置均保留。

## 5. 搜索后端选择

`search.enabled=false` 关闭联网。开启时可选择：

| search.backend | 行为 |
| --- | --- |
| `external` | 沿用 DeepSeek Search/Fetch，通过当前聊天模型的函数调用执行；旧配置未填 backend 时使用此模式 |
| `native` | 由所选供应商执行内置搜索；不支持时启动校验报错 |
| `auto` | Responses/Anthropic 且连接未禁用原生搜索时选 native，其余选 external |

`auto` 在请求前根据配置选择路线，不会在 HTTP 失败后切换供应商或搜索后端。原生搜索使用当前主回复模型的密钥，不创建额外 SearchClient。主动参与判断、后台记忆、头像分类及现有的主动短接话，不会因此自动获得联网工具。

原生搜索沿用 `max_requests`、`max_tool_rounds`、`total_timeout_seconds` 和反馈开关。OpenAI 将调用预算映射为 `max_tool_calls`；Anthropic 使用 `max_uses`，并在有界续接中减少剩余预算。达到预算、响应仍未完成、结果为空或工具错误时明确返回受限结果，不把草稿当最终答复。暂停续接只在原截止时间内进行，不独立重试。

网页引用转成正文中的普通 URL，保留可点击的来源，不向群里发送 Markdown、原始引用编号、思考内容或签名。状态和日志只包含计数、阶段及安全错误码。当前仍为非流式请求；反馈开启时只能在收到已发生搜索的响应后产生进度事件，默认仍关闭反馈。

Responses 的工具、输入 Items 和无状态续接依据 [OpenAI 官方迁移文档](https://developers.openai.com/api/docs/guides/migrate-to-responses)，搜索预算和引用依据 [Responses 参考](https://developers.openai.com/api/reference/resources/responses/methods/create) 与 [联网文档](https://developers.openai.com/api/docs/guides/tools-web-search)。Anthropic 的暂停、工具状态和原生搜索依据 [server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools) 和 [web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)。

### 外部搜索的密钥

选择 external 时仍调用 DeepSeek Search/Fetch。可显式指定自己的密钥环境变量：

```json
{
  "search": {
    "api_key_env": "DEEPSEEK_SEARCH_API_KEY"
  }
}
```

该变量同样放在 `.env`。未配置此项时保留旧行为，使用 `deepseek.api_key_env`；显式配置后若变量缺失，明确报缺少搜索密钥，不回退到聊天密钥。搜索工具仍受现有查询范围、次数、来源和总时限约束。

## 6. 思考、图片与协议边界

- Responses：`thinking=disabled` 转换为 `reasoning.effort=none`；开启时支持配置 `low/medium/high/xhigh/max`，具体可用档位由模型决定。使用 `max_output_tokens`，包含可见输出和思考预算；思考开启时不发送 `temperature`。默认 `store=false`，加密推理 Items 在当前工具循环内完整回传，不持久保存云端 response_id。
- Anthropic：默认 `thinking_mode=adaptive`，思考开启时用 `output_config.effort`。旧模型可明确选 `manual`，设置 `thinking_budget_tokens`（至少1024、小于 max_tokens）并将 `reasoning_effort` 设为 null。不把 max 自动换成猜测的预算。
- Anthropic 的 `thinking=disabled` 默认发送 disabled；个别模型要求使用最低的 `between_tools` 模式时，可明确设置 `thinking_mode=between_tools`。该模式可能在工具间思考，不能理解为完全关闭；模型能力以供应商为准，不做静默映射。
- Anthropic 的 `anthropic_version` 支持 `2023-06-01`（适配器兼容默认）和 `2026-10-21`；示例使用后者。新版 API 移除了 temperature，适配器会省略该字段。`anthropic_auth=api_key` 使用标准头，Bearer 网关可显式改为 bearer。
- Anthropic 原生搜索默认 `web_search_20250305`。可配置 `anthropic_web_search_version` 为 `web_search_20260209` 或 `web_search_20260318`，这些版本使用 direct 调用，不额外启用动态过滤代码执行。
- 原始图片数据继续由微信库校验、解密；适配器只转换已准备好的图片块。Responses 保留 original/high/low/auto 细节配置，Anthropic 转为 base64 或 HTTPS source，不重新截图或降级到缩略图。

思考设置和版本差异分别参见 [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[GPT-6 参数说明](https://developers.openai.com/api/docs/guides/latest-model)、[Anthropic thinking](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)、[legacy thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) 与 [API 版本](https://platform.claude.com/docs/en/api/versioning)。签名思考块、redacted_thinking 和加密推理只作为不透明续接状态，不用于正文、记忆事实或日志。

## 7. 添加其它协议

其它协议实现以下小接口，并在 `providers/__init__.py` 中登记名字：

```python
class ProviderAdapter(Protocol):
    native_search_supported: bool
    def endpoint(self, base_url: str) -> str: ...
    def headers(self, api_key: str, settings: dict | None = None) -> dict[str, str]: ...
    def encode(self, settings: dict, request: ModelRequest) -> dict: ...
    def decode(self, response: object) -> ModelResult: ...
    def assistant_message(self, message: dict) -> dict: ...
```

内部 `messages`、图片块和函数工具定义保留现有字典结构。适配器在 `encode/decode` 中转换系统提示、图片、工具调用与结果，映射统一结束原因和用量。`ModelRequest.native_search` 是显式的原生搜索预算；普通请求为空，不能仅因配置里启用了搜索就自行添加工具。`ModelResult.search` 返回来源、引用与搜索状态，编排层再生成不含正文的安全统计。

需要续接的供应商专属状态可保存在返回消息中，再由 `assistant_message/encode` 正确回传；不能当作回复正文、记忆事实或日志内容。切换供应商时开启新的模型请求链，不混用旧供应商的续接状态。本轮没有添加跨请求会话持久化。

`ModelClient(..., adapter=...)` 可显式注入适配器，用于合成测试或本地集成；常规配置走显式注册表，没有动态加载任意模块、远程代码或自动供应商发现。

## 8. 回归与边界

回归覆盖配置继承和选择、各协议的图片/工具/认证、签名与加密思考续接、不同协议复用外部搜索、原生搜索、Anthropic 200 错误与 pause_turn、预算耗尽不重提、私有 URL、不完整输出、用量归一化和 worker 脱敏。思考用量是输出用量的子集，缓存用量是输入组成或子集，统计不会把思考再次加到总输出。

测试只使用合成 HTTP 和工具响应，没有调用真实模型、发送群消息或改动 Linux 服务。原有工作进程继续负责取消；任务队列继续负责并发、重试和恢复。

[统一客户端源码](../wxbridge/ai/client.py) · [适配器接口](../wxbridge/ai/providers/base.py) · [基础兼容回归](../tests/test_model_providers.py) · [原生协议回归](../tests/test_native_model_protocols.py)
