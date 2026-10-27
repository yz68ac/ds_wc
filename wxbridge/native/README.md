# native 模块：接口、权限与注入

这一层负责 Linux 微信内部的文本发送。接收消息仍由 `wechat/store.py` 读取数据库，排队、冷却和发送确认仍由 `app` / `storage` 负责；`native` 不是完整的微信协议客户端。

## 文件分工

| 文件 | 在哪里运行 | 负责什么 |
| --- | --- | --- |
| `command.py` | 调用方进程 | `python3 -m wxbridge native-api` 命令 |
| `client.py` | 调用方进程 | Python `NativeClient`，通过私有 socket 提交与查询 |
| `api.py` | 机器人主进程 | 校验本机调用者、目标允许清单、请求 ID；将外部请求写入同一队列 |
| `protocol.py` | Python 进程 | 有界 JSON/Unix socket 协议、固定错误、UTF-8 分片 |
| `sender.py` | 受控发送工作进程 | 将队列中已登记的发送意图交给 helper，不访问桌面或数据库 |
| `helper.py` | root 所有的隔离 helper | 校验目标、微信进程、ELF hash 和 profile；每次发送启动受限子进程并短时附加 Frida |
| `agent.js` | 微信进程内的 Frida JS 运行时 | 在主线程定位后台服务、构造收件人/正文、创建并订阅发送任务 |
| `profile.json` | helper 与注入脚本读取的数据 | 固定客户端版本、架构、hash 和函数指纹；不是用户配置 |

## 一条回复如何经过这里

```text
@ / ds 关键词 / 主动接话 → 模型生成 ─┐
                                   ├→ 主队列 → 持久化发送意图
外部 CLI / NativeClient → api.py ───┘          ↓
                                 native_send 工作进程 / sender.py
                                              ↓ 私有 helper socket
                                  root helper → 受限子进程
                                              ↓ Frida attach
                                  微信进程内执行 agent.js
                                              ↓ 微信原有调度器
主进程读取新增本人发出记录 ←─────────────── 微信写入数据库
              ↓
       确认分片、推进队列和冷却
```

普通回复选择 `ui.backend=native_service` 才走这里；新安装默认仍保留桌面后端。外部 native-api 请求使用原生通道，不调用模型。提交返回与数据库确认是两个阶段，状态不明时保留意图、暂停发送，不回退桌面重发。

## agent.js 是怎么进去的

`helper.py` 在微信进程之外执行以下过程（省略校验和异常处理）：

```python
session = frida.get_local_device().attach(pid)
source = profile_and_request_constants + agent_path.read_text()
script = session.create_script(source)
script.load()
script.exports_sync.arm()
```

Frida 将它自己的原生组件注入目标进程，并在里面提供 JavaScript 运行时。Python 把脚本源码作为字符串交给 Frida；`script.load()` 使其在微信进程内执行。不是把 JS 文件交给微信正常加载，也不修改磁盘上的微信主程序。

`arm()` 是脚本公开的 RPC 入口。它通过 GLib 安排一次主线程回调，再用 `NativeFunction(模块基址 + RVA, 返回类型, 参数类型)` 调用已经核对的原生函数。这里的 JS 具有进程内内存访问能力，不是浏览器网页脚本。请求结束后 helper 取消未执行回调并解除附加；在途回调的存活由 pin 和销毁回调管理。

这部分是注入调用；消息是否真正发出，仍以主进程后续核对数据库为准。

## 源码与部署资产

这里的 `helper.py`、`agent.js`、`profile.json` 是受版本管理的源文件。正式部署由 [安装器](../../scripts/install-native-helper.py) 将它们复制到 root 所有的 `/opt/wxbridge-native-helper`，配合隔离 Frida 环境和 root 私有配置使用。修改项目中的文件不会自动更新已安装 helper；升级需核对版本、备份并同步相应资产。

客户端当前固定为 Linux x86-64 微信 4.1.13.26。后台服务按每次请求重新定位，不缓存聊天视图、不扫描聊天视图堆，也不依赖当前聊天窗口。微信本体的启动、登录和恢复继续使用现有虚拟显示环境。

配置、CLI、权限、幂等与失败状态见 [发送接口](../../docs/NATIVE_API.md)；两轮研究结论和已验证边界见 [分析总结](../../docs/NATIVE_ANALYSIS.md)。
