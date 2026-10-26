# 原生图片下载 SDK

`wechat_linux` 已封装微信内部图片下载入口，作为**显式选择、默认关闭**的实验能力。本轮只修改库、测试及文档；bot 没有接入，Linux 线上代码、服务与配置没有更新。

先前研究已在 Linux 微信 4.1.13.26 x86_64 的一个冷缓存样本上验证：不切换会话、不打开图片，取得 850×1276 的清晰缓存。当前封装经过合成故障回归，尚未对新版 helper 进行真实进程验收；不能把单样本研究结果理解为所有图片都已支持。

## 1. 调用方式

安装包为 `wechat-linux-adapter`，导入名为 `wechat_linux`。库不会安装 Frida、启动 helper、读取 `.env` 或修改服务。

```python
from wechat_linux import (
    ImageDownloadError, NativeImageDownloader, SessionConfig, WeChatSession,
)

chat_id = "example@chatroom"
downloader = NativeImageDownloader(
    helper_socket="/run/myapp/native-helper.sock",
    allowed_chats=[chat_id],
    expected_account="wxid_example",
)
cfg = SessionConfig(
    db_dir="/var/lib/myapp/wechat/wxid_example/db_storage",
    keys_file="/run/myapp/keys.json",
    decrypted_dir="/run/myapp/decrypted",
    allowed_chats=(chat_id,),
    expected_account="wxid_example",
)

with WeChatSession(cfg, image_downloader=downloader) as wechat:
    # reference 来自该群真实 Message.reference；不要由模型编造。
    try:
        image = wechat.fetch_image(
            chat_id, reference,
            download=True, download_backend="native",
            request_id="myapp:image-001", timeout=30,
        )
        consume(image.data, image.info)
    except ImageDownloadError as exc:
        record_safe_error(exc.code, exc.requested)
        # 不换新 ID、不自动改走桌面、不重复提交。
```

示例中的 `reference`、`consume` 和 `record_safe_error` 由应用提供。原引用绑定、解密副本和缓存只由普通权限调用方处理。会话继续遵守单一读库所有者锁，不能与 bot 并发刷新同账号解密副本。

`download=False` 仍只读缓存；原有 `download=True` 默认仍是桌面兼容下载。只有明确传入 `download_backend="native"` 并注入 downloader 才使用新入口。原生下载失败会抛出异常，没有自动桌面降级。

当前原生下载 `timeout` 要求整数 5～30 秒；downloader 默认 30，会话默认 20，示例显式设置为 30。调用是同步等待，应用若需要持续轮询消息，应在完成来源绑定后把下载移至自己的受控工作单元；不要跨线程并发使用同一个 `WeChatSession` 或 downloader。`NativeImageDownloader.download(binding, ...)` 也可直接接收由 `WeChatStore.image_binding()` 产生的可信绑定，本身不查数据库。

## 2. helper 的独立开关

旧 helper 配置不含 `image_download` 时，新功能保持关闭，不加载 image worker，也不创建图片工作线程。禁用配置只能是 `"image_download": {"enabled": false}`。

以后由应用所有者明确启用时，需要将下列文件放入**已有 root 所有、不可被应用用户改写的可信 assets 目录**：

```text
helper.py                 # 使用本轮兼容版
agent.js / profile.json   # 原文字发送资源，保持原用途
image_worker.py
image_contract.py
image_agent.js
image_adapter.c
image_profile.json
```

在现有 helper 配置中增加：

```json
"image_download": {
  "enabled": true,
  "expected_account": "wxid_example",
  "account_root": "/var/lib/myapp/wechat/wxid_example",
  "timeout_seconds": 30
}
```

这是配置片段，不是完整配置文件。保留原 `socket`、`uid`、`allowed_chat_ids`、`assets` 和 `worker_timeout_seconds`。账号目录必须属于配置中的应用 UID、路径无符号链接，末级目录为该账号或带后缀的账号目录。当前仅允许白名单中的群聊。

helper 在 attach 前检查进程 UID、固定可执行路径 `/opt/wechat/wechat`、ELF SHA-256 和进程启动标识；agent 再检查 x64 ABI、函数字节与回调 RTTI。账号还通过该进程当前打开的 `db_storage` 文件根目录核对；不满足条件就拒绝。切换账号、微信升级后不能继续套用原地址。

当前安装和 bot 发布脚本仍只部署文字发送资源，**不会安装这些图片资源，也不会打开开关**。未来启用前必须另行实现部署与真实验收。

### 服务停止必须排空回调

原生 C 回调可能挂起在微信协程内。Python 等待结束、IPC 断开、取消标志或 helper 收到 SIGTERM，都不能证明回调已经销毁。worker 会继续查询状态，直到协程句柄释放且克隆引用归零，才卸载 agent。

当前生产安装器生成的 `TimeoutStopSec=8` 不适合启用此功能。以后启用图片 worker 前，需要先给对应 helper 服务设置允许完整排空的停止策略，例如 `TimeoutStopSec=infinity`，并测试部署停止流程；不能在排空期间强杀 helper、让 watchdog 强制重启它，或手动卸载 agent。现有安装器本轮没有修改，线上也未启用。

若 RPC 永久失联、微信进程仍存在，worker 会保持占用并拒绝新图片任务；不能靠重复请求恢复。应由维护者核对进程状态，必要时正常退出并重启微信。应用请求的 30 秒是等待预算，不是强制销毁原生回调的期限。

## 3. 来源、结果与重试边界

IPC 只接受请求 ID、账号、群 ID、原图片 server_id、发送者稳定 ID、图片 MD5 和超时，不接受客户端代码、路径、profile 或内存地址。helper 复用微信自己的数据库工作项取得原始核心记录，再核对群、类型、server_id、发送者与摘要；它不打开解密数据库，不产生第二个解密副本。

通过后在微信真正的协程环境调用完整图片下载入口。C 适配器保留共享所有权，不修改 TLS、不伪造 UI 消息对象。原生成功需要完成 promise 有值、下载结果为 `[0, 0]`、清理成功且回调引用归零。SDK 随后仍须从**原绑定缓存路径**读出、解密和校验完整图片，才返回 `Image`；缩略图、预览图、CDN 任务创建或函数返回均不算清晰图成功。

若缓存已有合格 original/cached 图片，可直接返回，不发送下载请求。最大返回数据默认 8 MiB，超过预算、解码失败或只拿到缩略图不会假报下载成功。已验证的原缓存仍可能不是发送者最初的原始文件，不能把 cached 一概叫原图。

图片 helper 全局按应用 UID 串行，只允许一个活动原生图片任务。调用方与 helper 各自保留最多 4096 个请求去重记录，不淘汰旧 ID；满后返回明确错误。相同 ID、相同来源再次调用只查询状态；来源变化返回冲突。超时只取消尚未开始的阶段，已运行的服务端任务可能继续产生缓存。

```python
# 只读；不会创建或重新提交下载。
status = downloader.get_status("myapp:image-001")
```

`requested` 为 `False` 表示已确认未请求下载，为 `True` 表示确实调用过，为 `None` 表示不明确；即使 `True` 也不等于缓存验收成功。

去重历史是**内存态**，本轮没有新建持久下载队列。应用或 helper 重启后，应先使用旧 ID 查询并重新只读检查缓存；`native_image_request_missing` 代表无法恢复结论，不代表没有执行。不要创建新 downloader 后立即重放旧下载，也不要换 ID 自动重试未知结果。持久任务恢复由未来调用方接入时处理。

## 4. 文件与验证

| 文件 | 作用 |
| --- | --- |
| `wechat_linux/native/image_download.py` | 普通权限 SDK、root 对端核对、缓存验收 |
| `wechat_linux/native/image_contract.py` | 请求、错误与生命周期协议 |
| `wechat_linux/native/image_worker.py` | 限流、去重、账号核对、attach 与排空 |
| `wechat_linux/native/image_agent.js` | 核心记录来源验证、原生协程和 future 状态机 |
| `wechat_linux/native/image_adapter.c` | libc++ 回调 ABI 与原生协程栈适配 |
| `wechat_linux/native/image_profile.json` | 固定版本、ELF 摘要、指纹和必要 RVA |

离线回归覆盖：非法绑定、版本不符、错误账号、重复请求、IPC 结果不明、取消前后、回调克隆晚释放、RPC 短暂失联、进程退出、原生已完成但缓存仍不清晰、默认配置关闭，以及独立 wheel 导入。JS 模拟器执行包内真实 agent 状态机，使用合成内存与 native stub，不访问微信。

慢网络、撤回和过期资源、不同账号客户端状态以及新版封装的真实进程稳定性，仍需未来启用前实测。没有通过降低来源校验、盲目重试或恢复桌面点击来掩盖这些边界。

研究过程见 [原生图片接口分析](NATIVE_IMAGE_ANALYSIS.md)，库的其它 API 见 [WeChat Linux Adapter](WECHAT_LIBRARY.md)。
