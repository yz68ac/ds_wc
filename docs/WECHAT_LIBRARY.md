# WeChat Linux Adapter

本仓库的微信能力库，发行名 `wechat-linux-adapter`，Python 导入名 `wechat_linux`，当前版本 0.1.0。支持 Linux；Windows 可运行解析和合成测试，不支持连接 Windows 微信。

## 安装与边界

在仓库根目录运行：

```bash
python3 -m pip install .
# 需要本地读库、解压和解密时
python3 -m pip install '.[database]'
```

要求 Python 3.11 或以上。默认没有 Python 第三方依赖；读库可选 pycryptodome、zstandard。图片的 WXGF 解码依赖客户端兼容的原生组件；桌面兼容下载需要已登录微信、X11、D-Bus、AT-SPI、窗口管理器、PyGObject/GdkPixbuf 和 xdotool。另有默认关闭的原生图片下载 SDK，使用受约束 helper，不操作图片窗口；微信本体启动与登录仍可能需要虚拟显示。库不安装系统组件，不自动登录、不提取密钥、不启动服务、不读取 `.env`。

wheel 只包含 `wechat_linux` 及原生发送/图片下载所需的 JS、C 和版本 profile；不包含机器人、模型提示词、配置、聊天记录、记忆、凭据或微信二进制。暂未选择许可证，发布或转授权前需要项目所有者明确许可。

对应版本的微信主程序和原始 deb 已另存为 [本地备用资源](../resources/wechat/README.md)，附版本、来源及 SHA-256 清单；大文件不进入 wheel，运行时不会自动执行或替换它们。

机器人从仓库运行前需将 `src` 加入 `PYTHONPATH`，再运行 `python3 -m wxbridge`，直接使用库代码。旧 `wxbridge.wechat.*`、`wxbridge.native.*` 等入口保留兼容导入；它们不再各持一套微信实现。`wxbridge.ai.images` 只组装模型图片输入。

## 模块分工

| 模块 | 能力 |
| --- | --- |
| `messages` | 引用、真实 @ 元数据、拍一拍解析；稳定成员 ID |
| `storage` | 客户端数据库发现、SQLCipher/WAL、联系人与消息查询、管理员密钥扫描 |
| `media` | 绑定本账号缓存、图片校验及内存解密、WXGF 解码 |
| `desktop` | 可选桌面兼容操作、绑定来源的清晰图下载 |
| `native` | 本地协议、客户端、发送提交、显式原生图片下载、独立 helper、版本 profile |
| `sending` | 发出记录核对、独立项目持久发送意图 |
| `transport` | 可嵌入主进程的非阻塞 Unix socket；业务处理器由调用方提供 |
| `runtime` | 私有 tmpfs/noswap 校验、单一读库所有者锁 |

DeepSeek、联网搜索、人格、好感度、记忆、触发规则、主动参与与机器人任务队列仍属于 `wxbridge`。库不决定是否回复群聊。

## 读消息

下面的路径和 ID 都是示例；应使用已验证的当前账号和群 ID。`decrypted_dir` 必须与该微信账号其它读库进程使用同一目录，放在本用户独占的 `tmpfs,noswap` 中，权限 0700。调用者必须在自己的状态库事务中保存游标和业务任务。

```python
from wechat_linux import SessionConfig, WeChatSession

cfg = SessionConfig(
    db_dir="/var/lib/myapp/wechat/account/db_storage",
    keys_file="/run/myapp/keys.json",
    decrypted_dir="/run/myapp/decrypted",
    expected_account="wxid_example",
    allowed_chats=("example@chatroom",),
)
with WeChatSession(cfg) as wechat:
    batch = wechat.read_messages("example@chatroom", after=100, limit=100)
    for message in batch.messages:
        # 使用 message.sender_id，昵称只用于显示
        process(message)
    save_cursor_with_tasks(batch.cursor)
```

会话持有 `decrypted_dir/.listener.lock`，与机器人共用同一锁；冲突抛出 `OwnerBusyError`。一个进程内也应由一个线程/事件循环串行访问会话。不要为同一账号另建解密目录绕过锁。底层 `WeChatStore` 是兼容的高级接口，直接使用它时须自行持有该锁。

`Message` 提供 `chat_id/local_id/server_id/sender_id/sender_name/type/time/text/mentioned_users/reference`，`raw` 保留完整解析字段。`mentioned_users=None` 表示无法获得元数据，不能当成“确定没有 @”。消息正文、引用和图片不出现在默认对象 repr 中。

## 发送与确认

同一主机已有机器人时，优先复用其 Unix socket，避免第二个进程读库：

```python
from wechat_linux import NativeClient

client = NativeClient("/run/wxbridge-trial/native-api.sock")
result = client.send_text("example@chatroom", "测试正文", request_id="myapp:request-001")
# 返回的是入队结果。之后用同一个 request_id 查询，直到 confirmed。
status = client.get_status("myapp:request-001")
```

独立项目可以使用已安装的隔离 helper；库不会给当前项目安装 Frida 或启动 root 服务：

```python
from dataclasses import replace
from wechat_linux import NativeSender, WeChatSession, DeliveryState

sender = NativeSender("/run/myapp/native-helper.sock", ["example@chatroom"])
cfg = replace(cfg, outbox_file="/run/myapp/sdk-outbox.sqlite3")
with WeChatSession(cfg, submitter=sender) as wechat:
    receipt = wechat.send_text("example@chatroom", "测试正文", request_id="myapp:request-002")
    # 交回主循环，继续接收消息，稍后核对；不要阻塞等待或重新发送
    receipt = wechat.confirm(receipt.request_id)
    if receipt.state == DeliveryState.CONFIRMED:
        mark_delivered(receipt.ack_id)
```

调用返回 `submitted` 只表示内部函数提交过。只有目标会话里出现基线之后、本账号发出、文字完全一致的记录才返回 `confirmed`。超时或传输不明为 `unknown`，继续核对，不自动重发。进程重启先用原 ID 查询/确认；相同 ID 不重发，不同正文复用 ID 会被拒绝。不得在不明结果后换新 ID 绕过阻塞。

`NativeSender` 内部使用独立的 `NativeOptions(helper_socket, allowed_chats, max_text_bytes=4096)`；不需要机器人配置或业务队列。原有应用 `submit(cfg, ...)` 和 `readiness(cfg)` 仍兼容，在边界转换配置。`NativeSender.cfg` 保留兼容配置快照，新代码使用 `sender.options`。

独立发送意图库在提交前写意图，权限 0600、账号绑定、单实例锁；同时只允许一个未确认意图，避免同文案歧义。默认保留最多 4096 个请求去重记录，满后明确报错，不淘汰去重记录。`not_submitted` 也保留原 ID 结果；处理明确失败后，由业务决定是否用新 ID 发起新任务。该库不替代机器人已有的任务队列。

意图库和解密副本随整机重启清空；跨整机重启的业务去重应由应用持久保存，不能假定客户端进程恢复等于送达。

## 图片与能力声明

使用从 `Message.reference` 取得的原始引用，调用 `wechat.fetch_image(chat_id, reference, download=False)`；返回 `Image.data` 与 `Image.info`，后者明确区分 original、cached、preview、thumbnail。默认只读缓存；`download=True` 才允许在受锁保护的桌面环境中打开绑定图片。若下载失败会返回原缓存和失败原因，不能把缩略图当成原图。详细实现见 [图片下载说明](IMAGE_DOWNLOAD.md)。

`SessionConfig.runtime_dir` 可显式指定桌面图片下载锁所在的私有内存目录，默认取 `decrypted_dir` 的上一级，与旧版位置一致。库的缓存读取传递图片大小上限，桌面下载传递运行目录，不再构造假的机器人 `queue.file`；原有底层 `cfg` 入口继续兼容。

原生图片下载已独立封装为 `NativeImageDownloader`。注入会话后，显式传入 `download=True, download_backend="native", request_id=...` 调用；失败抛出 `ImageDownloadError`，不自动降级或重试。它要求独立启用的可信 helper 资源及安全排空策略，bot 已支持显式选择 native 下载，helper 需通过受控发布显式安装图片资源和开启开关。完整示例和边界见 [原生图片 SDK](NATIVE_IMAGE_SDK.md)。

`capabilities()` 只读取随包的声明，不连接微信。当前 native profile 为 Linux 微信 4.1.13.26、x86_64；helper 会校验 ELF SHA-256 与指令签名。客户端更新后必须重新分析和测试，不能直接沿用偏移。声明的支持不等于当前已登录或依赖已安装。

高级 socket 服务 `NativeServer(options, service)` 的 `service.handle(request)` 在主事件循环执行，`tick()` 非阻塞收发；调用方须持有会话锁、负责入队和业务去重。机器人适配器位于 `wxbridge.app.native_gateway`。

## 开发验证与后续拆仓

版本升级和学习用的离线 ELF 检查、旧/新样本比较、指纹候选搜索、资产盘点和可选反汇编，见[版本与二进制离线分析](WECHAT_ANALYSIS.md)。这些工具不连接微信进程，也不自动修改版本档案。

```bash
python3 scripts/test-offline.py
python3 scripts/check-repository.py
python3 -m pip wheel --no-deps . --wheel-dir /tmp/wechat-linux-wheel
python3 scripts/smoke-wheel.py /tmp/wechat-linux-wheel/wechat_linux_adapter-0.1.0-py3-none-any.whl
```

合成测试覆盖导入隔离、兼容类型、单实例、账号隔离、发送中断恢复、晚到确认、发出记录不匹配、图片与发布资产迁移。不需要真实聊天、登录、模型请求或群发。正式收发仍需已登录客户端及兼容 helper。

CI 另外在 Python 3.11 和 3.14 构建 wheel，在全新虚拟环境无依赖安装，并从仓库外用隔离模式导入全部库模块和读取 native 资源，避免源码测试通过而发行包漏文件。能力声明只描述库本身；应用是否启用某项能力应查看应用状态。

0.1 是同仓库可安装提取：先保持线上行为，再考虑独立仓库和版本发布。未来拆仓只需迁出 `wechat_linux`、构建配置、本文及 SDK 测试；机器人固定库版本。不要同时重写微信协议或引入新常驻服务。
