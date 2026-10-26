# 模型供应商适配

本轮为以后更换模型供应商做轻量抽象。业务统一使用 `ModelClient`、`ModelError`；供应商负责请求字段、HTTP 地址与认证、响应解析和工具续接。微信、人格、记忆来源校验、队列和并发调度保持原分工。

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
    __init__.py                显式供应商注册表
  search.py                    工具执行、搜索预算和来源处理
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

## 3. 更换兼容供应商

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

### 搜索后端独立

本轮没有替换 DeepSeek Search/Fetch 服务。换聊天模型不意味着搜索也自动换供应商。可为现有搜索后端显式指定自己的密钥环境变量：

```json
{
  "search": {
    "api_key_env": "DEEPSEEK_SEARCH_API_KEY"
  }
}
```

该变量同样放在 `.env`。未配置此项时保留旧行为，使用 `deepseek.api_key_env`；显式配置后若变量缺失，明确报缺少搜索密钥，不回退到聊天密钥。搜索工具仍受现有查询范围、次数、来源和总时限约束。

## 4. 添加不同原生协议

暂未实现 Anthropic Messages、Responses 或其他原生协议。以后接入时实现以下小接口，并在 `providers/__init__.py` 中登记名字：

```python
class ProviderAdapter(Protocol):
    def endpoint(self, base_url: str) -> str: ...
    def headers(self, api_key: str) -> dict[str, str]: ...
    def encode(self, settings: dict, request: ModelRequest) -> dict: ...
    def decode(self, response: object) -> ModelResult: ...
    def assistant_message(self, message: dict) -> dict: ...
```

本轮为减小改动，内部 `messages`、图片块和工具定义保留现有 Chat Completions 风格的字典结构。它是内部约定，不要求上游使用相同网络协议。新适配器在 `encode/decode` 中转换系统提示、图片、工具调用与结果，映射统一结束原因和用量；业务提示词、记忆模块和搜索执行器不用处理供应商字段。

需要续接的供应商专属状态可保存在返回消息中，再由 `assistant_message/encode` 正确回传；不能当作回复正文、记忆事实或日志内容。切换供应商时开启新的模型请求链，不混用旧供应商的续接状态。本轮没有添加跨请求会话持久化。

`ModelClient(..., adapter=...)` 可显式注入适配器，用于合成测试或本地集成；常规配置走显式注册表，没有动态加载任意模块、远程代码或自动供应商发现。

## 5. 回归与边界

回归覆盖旧配置和别名、角色密钥隔离、图片与工具不被改写、思考状态续接、不同合成网络协议复用原搜索循环、截止时间耗尽不发送请求、错误和对象 repr 脱敏、搜索独立密钥，以及原有记忆 JSON 截断检查。

测试只使用合成 HTTP 和工具响应，没有调用真实模型、发送群消息或改动 Linux 服务。原有工作进程继续负责取消；任务队列继续负责并发、重试和恢复。

[统一客户端源码](../wxbridge/ai/client.py) · [适配器接口](../wxbridge/ai/providers/base.py) · [合成兼容回归](../tests/test_model_providers.py)
