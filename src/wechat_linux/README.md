# wechat_linux 代码地图

这个包是微信 Linux 适配和分析库；`wxbridge` 是使用它的群聊机器人。库可单独打包，模型、群聊人格、记忆和机器人调度属于 `wxbridge`。

| 目录或文件 | 作用 | 从哪里开始 |
| --- | --- | --- |
| `analysis/` | 离线检查 ELF、比较版本、搜索指纹候选和可选反汇编 | [分析说明](../../docs/WECHAT_ANALYSIS.md)、`analysis/cli.py` |
| `storage/`、`messages/` | 数据库、解密、消息与引用解析 | `storage/store.py`、`messages/references.py` |
| `native/` | 原生发送和图片下载、helper、注入脚本及版本档案 | `native/sender.py`、`native/image_download.py` |
| `desktop/` | 虚拟桌面和无障碍兼容操作 | `desktop/ui.py`、`desktop/image_download.py` |
| `media/` | 图片缓存、解密和格式处理 | `media/images.py` |
| `sending/` | 发送意图、分片和发出记录确认 | `sending/outbox.py`、`sending/confirmation.py` |
| `transport/`、`runtime/` | 本地通信、运行目录和互斥锁 | `transport/server.py`、`runtime/locking.py` |
| `session.py` | 给独立调用方使用的会话接口 | [库接口说明](../../docs/WECHAT_LIBRARY.md) |

从仓库根目录查看内置版本档案：

```bash
python -m wechat_linux analysis profiles
```

版本分析是只读研究工具；候选地址还需要结合实际指令、对象布局和运行时行为验证。旧 `wxbridge.wechat`、`wxbridge.native` 中的部分文件是兼容入口，具体微信实现以本包为准；机器人自己的队列和调度仍留在 `wxbridge`。
