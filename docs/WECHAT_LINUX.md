# wechat_linux 当前状态与调用方式

更新：2026-10-29。当前源码发行包为 `wechat-linux-adapter 0.2.0`，Python 导入名为 `wechat_linux`，要求 Python 3.11+。

本文描述当前源码接口。历史实验、部署命令和逆向细节在 [参考资料](reference/README.md)；历史实测不等于所有分支或当前服务器均已验证。

## 1. 定位与运行条件

`wechat_linux` 提供本地数据库读取、消息解析、媒体处理、原生提交和离线分析。群/私聊实例、收件人授权、FIFO 任务队列、模型调用、发送确认、记忆和自动恢复由上层 `wxbridge` 管理。

| 条件 | 当前要求 |
| --- | --- |
| Python | 3.11+ |
| 实际微信运行 | Linux；既有实机环境为 Debian 13 amd64 |
| 原生版本档案 | 微信 4.1.13.26，x86_64 |
| 原生调用 | 匹配档案的客户端，以及单独安装的 root helper / Frida 环境 |
| 数据库读取 | 当前账号有效密钥、pycryptodome、按需使用 zstandard |
| 桌面兼容操作 | X11、AT-SPI、xdotool；微信启动/登录仍使用现有虚拟显示环境 |

固定样本 SHA-256：

```text
2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132
```

档案匹配只说明版本和指纹相符。升级微信后需要重新研究与验证，不能只改版本号。库导入不自动登录、扫描密钥、读取机器人配置或启动 helper。

## 2. 当前支持程度

| 功能 | 源码状态 | 验证边界 |
| --- | --- | --- |
| 账号、联系人、会话和增量消息读取 | 已实现 | 既有 Linux 环境使用；调用方维护游标 |
| 群聊与 `wxid_` 私聊 | 读库支持；wxbridge 实例也接受两类 ID | 私聊已做目标解析和监听启动核对，完整自动收发仍需按场景验收 |
| 真实 @、引用回复 | 已实现解析 | 保留消息原始类型；上层决定如何参与业务 |
| 拍一拍 | 已实现解析 | 实时同步仍属实验边界 |
| 本地图片读取、DAT 解密、WXGF 解码 | 已实现 | 只读取来源绑定的缓存；清晰缓存不保证等于上传原文件 |
| 原生文字发送 | 已实现 | 既有版本有实机验证；返回只表示提交 |
| 原生 PNG/JPEG 发送 | 已实现接口和版本档案 | `runtime_verified=false`，尚未完成实机验证 |
| 原生图片下载 | 可选接口，默认关闭 | 历史有一个冷缓存样本成功；当前请求协议限定群聊，不可推定私聊也支持 |
| 桌面发送 / 下载兼容组件 | 已保留 | 依赖窗口与无障碍环境，由上层选用 |
| 启动登录与终端二维码 | 已实现接口及启动接入 | 本轮只检查已登录状态；冷启动、手机确认与二维码扫码待实机验收 |
| 类型 47 的自定义表情包 | 消息可读，仍归为 `non_text` | 尚无专用资源解码、语义识别或原生表情发送接口 |
| 语音、视频、文件发送及入群操作 | 未提供公共封装 | 不以 SDK 或微信内部存在相关能力视为已支持 |

`capabilities()` 返回代码声明及档案信息，不会检测账号登录状态。实际 helper 的 `NativeSender.health()` 可以查询已安装 helper 暴露的发送能力；本地源码变化不会自动更新已安装 helper。

## 3. 安装

从仓库根目录安装：

```bash
python -m pip install ".[database]"
# 只做静态分析时：
python -m pip install ".[analysis]"
```

基础包依赖为空，不包含 `provider` 或 `wxbridge`。微信二进制、密钥、账号数据、root helper 和系统服务需要单独准备。源码直接运行时，将仓库 `src` 加入 `PYTHONPATH`。

## 4. 调用方式总表

| 方式 | 入口 | 适用场景 |
| --- | --- | --- |
| Python 会话接口 | `WeChatSession + SessionConfig` | 独立程序组合读消息、读图片和发送 |
| Python 原生发送 | `NativeSender` | 显式收件人文字 / 本地 PNG、JPEG 提交 |
| Python 原生下载 | `NativeImageDownloader` | 已启用 helper 的来源绑定图片下载 |
| Python 读库 | `WeChatStore` | 联系人、会话、消息窗口、头像和附件绑定 |
| Python 能力查询 | `capabilities()` | 查看内置档案和声明能力 |
| 命令行离线分析 | `python -m wechat_linux analysis ...` | 查看 ELF、检查版本指纹、比较与搜索候选 |
| 机器人本地 API | `wxbridge.native.client.NativeClient` | 接入已运行的 wxbridge 队列及状态查询，属于上层应用 |

库自身没有 HTTP / TCP 服务，也不导出旧的 `wechat_linux.NativeClient`。不创建机器人队列、不提供自动发送重试或 `session.confirm()`。0.2.0 的确认与本地 API 服务器职责已迁入 wxbridge。

## 5. Python 会话：读取增量消息

以下路径和 ID 为示例，需要替换为自己账号的实际配置：

```python
from wechat_linux import SessionConfig, WeChatSession

config = SessionConfig(
    db_dir="/path/to/account/db_storage",
    keys_file="/path/to/private/keys.json",
    decrypted_dir="/path/to/private/decrypted",
    expected_account="wxid_your_account",
)

chat_id = "example@chatroom"  # 私聊使用实际 wxid_
cursor = 0  # 应由使用方保存；首次只接新消息时先取当前最新 local_id

with WeChatSession(config) as session:
    batch = session.read_messages(chat_id, after=cursor, limit=100)
    for message in batch.messages:
        print(message.local_id, message.type, message.message_kind)
    cursor = batch.cursor
```

`SessionConfig` 目前只有 `db_dir/keys_file/decrypted_dir/expected_account` 四项。会话确认账号并持有 reader；授权、排队与消费游标由使用方管理。解密目录要求归当前用户所有并限制权限；wxbridge 另行要求自己的私有 `tmpfs,noswap`。

`Message` 包含 `chat_id/local_id/server_id/sender_id/sender_name/type/raw_type/time/text`，以及 `message_kind/mentioned_users/reference/pat/image_md5/image_cache_key/raw` 等字段。引用消息保留原始类型，方便上层自行选择业务表现。

### 会话公共方法

| 接口 | 返回 / 行为 |
| --- | --- |
| `account_id` | 当前账号 ID |
| `read_messages(chat_id, after=..., limit=100)` | `MessageBatch(messages, cursor)`；limit 1–200 |
| `fetch_image(chat_id, reference, download=False, ...)` | `Image(data, info)`；下载时需注入 downloader |
| `send_text(chat_id, text, request_id=...)` | 调用注入的 sender，返回提交回执 |
| `send_image(chat_id, image_path, request_id=...)` | 调用注入的 sender，提交本地图片 |
| `close()` / 上下文退出 | 释放会话 reader 引用；注入资源仍由调用方持有 |

## 6. 原生文字和图片发送

```python
from wechat_linux import NativeSender, SessionConfig, WeChatSession

sender = NativeSender(
    "/run/your-app/native-helper.sock",
    max_text_bytes=4096,
    timeout_seconds=22,
)
print(sender.health())

config = SessionConfig(
    db_dir="/path/to/account/db_storage",
    keys_file="/path/to/private/keys.json",
    decrypted_dir="/path/to/private/decrypted",
    expected_account="wxid_your_account",
)

# 以下调用会实际提交微信消息；仅对自己的测试收件人执行。
with WeChatSession(config, sender=sender) as session:
    receipt = session.send_text("example@chatroom", "你好", request_id="text:unique-id")
    print(receipt.state)  # SUBMITTED，不是送达确认

    # 图片发送代码已实现，但当前版本仍标为尚未完成实机验证。
    # receipt = session.send_image(
    #     "example@chatroom", "/absolute/path/picture.png",
    #     request_id="image:unique-id",
    # )
```

也可以直接调用：

```python
receipt = sender.submit_text(chat_id, text, request_id="text:unique-id")
# 图片代码调用方式：
# receipt = sender.submit_image(chat_id, "/absolute/path/picture.jpg", request_id="image:unique-id")
```

| 约束 | 当前行为 |
| --- | --- |
| 目标 | 显式 `filehelper`、`wxid_` 或 `@chatroom` ID；helper 独立校验允许名单 |
| 文字 | 每次提交最多 4096 UTF-8 字节，按配置可调小；独立接口不自动分片 |
| 图片 | 本地 PNG/JPEG，最多 8 MiB；校验大小、格式和 SHA-256 |
| 图片生命周期 | 调用方保留源文件，并在确认完成前保持不变 |
| 结果 | `SendReceipt`，成功提交返回 `DeliveryState.SUBMITTED` |
| 不确定错误 | `SendError.submitted` 表示已提交、明确未提交或未知；未知不能作为安全重发依据 |
| 关闭 | `NativeSender` 按请求通信，没有独立的 `close()`；`WeChatSession.close()` 不销毁注入的 sender/downloader |

接收与提交链路：

```text
微信加密数据库 → 私有解密副本 → WeChatStore → MessageBatch
NativeSender → 本地 Unix socket → root helper → Frida agent → 微信内部提交
wxbridge → 记录发送意图 → 调用提交 → 查询本人发出记录 → 确认结果
```

回执中的 `SUBMITTED` 仅代表原生提交返回，不能当作消息已被对方收到。0.2.0 的库层不维护 outbox、账号发送闸门或确认游标；这些由应用承担。

## 7. 图片读取与可选原生下载

本地图片通过会话读取：

```python
with WeChatSession(config) as session:
    rows = session.read_messages(chat_id, after=cursor, limit=100)
    message = next(item for item in rows.messages if item.type == 3)
    image = session.fetch_image(chat_id, message.raw, download=False)
    print(image.info)  # 质量、尺寸、格式等；data 为图片字节
```

需要原生下载时，先独立配置并启用对应 helper，再注入：

```python
from wechat_linux import NativeImageDownloader

downloader = NativeImageDownloader(
    "/run/your-app/native-helper.sock",
    expected_account="wxid_your_account",
)
# 下载会触发真实微信内部请求；由使用方显式调用。
# with WeChatSession(config, image_downloader=downloader) as session:
#     image = session.fetch_image(
#         chat_id, reference, download=True, request_id="download:unique-id",
#         timeout=20, max_bytes=8 * 1024 * 1024,
#     )
```

底层还提供 `download(binding, chat_id=..., request_id=..., ...)` 和 `get_status(request_id)`。binding 必须由当前账号的原消息与附件信息核验；当前原生下载契约仅接受群 ID。请求 ID 绑定来源，重用时查询已有操作，不能在结果未知时换 ID 盲目重试。

## 8. 直接读库接口

`wechat_linux.storage.store.WeChatStore(db_dir, keys_file, decrypted_dir)` 提供以下方法：

| 方法 | 用途 |
| --- | --- |
| `self_wxid` | 当前账号身份 |
| `contacts()` / `sessions()` | 联系人及会话记录 |
| `resolve_chat(target)` | 解析稳定 ID 或已有显示名称；业务配置建议保存稳定 ID |
| `recent_messages(chat_id, limit=20)` | 最近消息 |
| `messages_after(chat_id, after_local_id, limit=50)` | 按本地 ID 增量读取 |
| `messages_in_window(chat_id, start, end)` | 时间窗消息 |
| `last_local_id(chat_id)` | 当前消息基线 |
| `avatar(username, max_bytes=...)` | 本地头像数据 |
| `image_binding(chat_id, reference)` | 原消息与本地图片缓存绑定 |
| `refresh(relative_db_path)` | 按数据库和 WAL 更新私有解密副本 |

`storage.keyscan.extract_keys` 是显式的本机进程密钥提取入口，要求相应进程读取权限；不会由会话自动执行。业务不应直接依赖 `_connect/_query_messages` 等私有方法。

## 9. 离线命令列表

```bash
python -m wechat_linux analysis profiles
python -m wechat_linux analysis inspect /path/to/wechat
python -m wechat_linux analysis check /path/to/wechat --profile all
python -m wechat_linux analysis compare /path/to/old /path/to/new --profile text
python -m wechat_linux analysis inventory --profile all
python -m wechat_linux analysis find /path/to/new --profile image --rva 0x6517dc0
python -m wechat_linux analysis disasm /path/to/wechat --rva 0x6517dc0 --bytes 64 --count 16
```

这些命令只读显式提供的 ELF 或内置资源，不附加微信进程、不发消息。`disasm` 需可选 Capstone，`--json` 可用于机器可读输出；候选地址仍需人工验证。

## 10. 启动自动登录与 SSH 终端二维码

可选依赖安装：`python -m pip install ".[login]"`。二维码解码使用 zxing-cpp，终端矩阵使用 qrcode；控件和截图使用系统 GI/AT-SPI/Gdk，不使用 OCR，也不需要 Pillow 或 OpenCV。

```python
from wechat_linux import LoginController

controller = LoginController(
    pid=12345,  # 调用方已启动的微信 PID
    db_dir="/path/to/account/db_storage",
    ui_options={"native_executable": "/opt/wechat/wechat"},
)
status = controller.inspect()
print(status.phase, status.reason)
```

控制器只操作已有进程，不启动或关闭微信、不创建后台线程。调用方管理生命周期与重试；同一控制器持续绑定 PID、启动时间、程序路径及当前 UID。

| 接口 | 用途 |
| --- | --- |
| `inspect()` | 获取 `LoginStatus(phase, pid, reason)`；已打开目标账号数据库时直接返回 `logged_in`，不读取聊天界面 |
| `click_login()` | 当前为 `login_ready` 时点击唯一可见登录按钮 |
| `open_wechat()` | 当前为 `open_ready` 时进入微信 |
| `refresh_qr()` | 当前为 `qr_expired` 且存在明确刷新按钮时刷新 |
| `read_qr()` | 只截取确认过的登录窗口，返回 `LoginQRCode`；截图和载荷不落盘 |
| `LoginQRCode.terminal_lines()` | 显式生成带四模块留白的黑白终端字符；二维码载荷不出现在默认 repr 中 |

状态包括 `waiting/login_ready/open_ready/phone_confirmation/qr_required/qr_expired/logged_in/unknown/client_stopped`。固定异常 `LoginError.code` 不包含控件正文或二维码；点击结果未知时不补点。

wxbridge 的账号启动层运行独立登录子进程，每 5 秒检查一次：每个微信进程的登录和进入按钮各自动尝试一次，登录后退出控制器，微信继续运行。运行时退出账号不会自动重新登录。去重状态以进程身份保存到私有 `login-state.json`；后台与终端查看共用 `login-ui.lock`。

通过 SSH 显式查看同一个会话：

```bash
ssh -t hotman "runuser -u wxbridge -- /opt/wxbridge/.venv/bin/python /opt/wxbridge/scripts/wechat-login.py"
```

已登录时直接报告状态；需要手机确认时提示确认；需要扫码时在终端的临时画面中显示二维码。二维码变化后重绘，明确过期时每次过期状态最多刷新一次。终端宽高不够会提示调整，不缩放二维码。Ctrl+C 或 SSH 断开只结束查看，不关闭微信。

只读状态可执行同一命令并追加 `--status`，无需分配 TTY；不会点击或截图。后台 `--background` 模式只输出状态码，不输出二维码、截图或控件原文。身份或界面不明确时保持等待，不切换账号。

2026-10-29 已增量部署到 hotman：只同步登录模块、两个入口脚本、共享运行逻辑及登录能力导出，保留服务器现有 0.1.0 其余代码。项目虚拟环境安装了 zxing-cpp 3.1.1 和 qrcode 8.2。只读检查返回 `logged_in`，微信、监听和原生 helper 均未重启；新后台入口在下次微信进程启动时生效。冷启动、手机确认、实际扫码及过期刷新尚待验收。

部署前文件及 SHA-256 清单保存在服务器 `/opt/wxbridge-backups/login-20261029-mvt2cjzv/`。如需恢复原有“启动后等待人工登录”的入口，可在服务器以管理员身份执行：

```bash
python3 /opt/wxbridge-backups/login-20261029-mvt2cjzv/rollback.py
```

恢复脚本先核对已部署文件未被后续修改，再还原启动脚本和能力导出；不会重启服务，保留未使用的登录模块与依赖。此入口恢复同样从下次会话启动生效。

## 11. 与 wxbridge 的边界

`wxbridge` 继续提供账号协调、群/私聊实例、三条模型通道、发送队列与确认，以及本地 API：

- `instance/<id>/config.json` 的 `enabled` 控制该实例；`chat` 保存稳定目标 ID。
- `reply/image/background` 每条通道串行，不同通道和实例可并行；provider client 首次使用时创建并复用。
- 私聊的新文字进入参与判断，通过后交给前台回复；类型 47 尚不进入该流程。
- 原生 helper 的安装资产与项目源码分离，更新源码后需要单独核对和部署 helper。
- 当前启动脚本会启动独立登录控制器，按上一节的范围推进登录。配置变化通常需重启监听，同一路径的系统提示词在下次生成时读取。
- 库层数据目录只要求私有权限；wxbridge 的队列、密钥和解密副本仍采用自己的内存挂载策略。

旧版 `SessionConfig.allowed_chats/outbox_file/runtime_dir`、库层 `NativeClient`、`ReliableSender`、`session.confirm()` 和 `wechat_linux.sending/transport` 已不属于 0.2.0 的公共接口。有关旧路径与实验结果，可查 [历史参考](reference/README.md)，新集成使用本文接口。
