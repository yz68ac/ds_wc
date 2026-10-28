> 模型配置与生命周期已重构：请以 [模型接口](MODEL_API.md) 为准。每群三条线程通道，旧 profile/persona 和模型总额度已移除。

# Linux 部署

已安装实例的差异部署和失败回滚可用[审阅修复说明](REVIEW_FIXES.md)中的`deploy-release.py`；默认仅打印计划，不执行更新。

源码运行依赖原生 Linux 微信。现有验证环境为 Debian 13 amd64、Python 3.13、微信 4.1.13.26；Windows 用于编辑与离线测试。微信客户端不随仓库分发，安装和升级后需要确认数据库与无障碍界面适配。

源码已迁至 `src/`。在仓库根目录交互运行 `python3 -m wxbridge` 前执行 `export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"`；`scripts/run-live.py` 和恢复脚本会自行配置源码路径。现有线上服务和恢复工具须按新的发布包一起迁移并检查，不能只复制部分目录。

## 约定与依赖

现有服务模板使用以下约定，修改路径时需一起检查脚本、服务和实际配置：

| 用途 | 默认约定 |
| --- | --- |
| 专用普通用户与 HOME | `wxbridge`、`/var/lib/wxbridge` |
| 代码与私密配置 | `/opt/wxbridge`、`config.json`、`.env` |
| 微信程序 | `/opt/wechat/wechat` |
| 内存运行目录 | `/run/wxbridge-trial`，私有 `tmpfs,noswap`，目录 0700 |
| 内存数据 | 密钥、解密副本、SQLite 队列、锁和状态；私密文件 0600 |
| 持久记忆 | `/opt/wxbridge/memory`，目录 0700、文件 0600 |

系统依赖包括 Python、PyCryptodome、zstandard、PyGObject/AT-SPI、Xvfb、Xauthority、D-Bus、fluxbox、xdotool、xclip，以及 `libde265`、FFmpeg 运行库。临时远程查看登录界面还需 x11vnc 和已有 VNC 客户端。Debian 可从系统包准备 `python3-pycryptodome`、`python3-zstandard`、`python3-gi`、`gir1.2-atspi-2.0`、`at-spi2-core`、`xvfb`、`xauth`、`dbus-x11`、`fluxbox`、`xdotool`、`xclip`、`libde265-0`、`ffmpeg`，再用 `python3 -m wxbridge env` 核对。

服务模板使用 `/usr/bin/python3`，依赖应能由该解释器导入。离线开发用的隔离 `.venv` 不会自动成为 systemd 运行环境；若改用虚拟环境，需同时解决 PyGObject 系统绑定并修改相关入口。不要向系统 Python 强行使用 `--break-system-packages`。

## 初次准备

1. 管理员建立专用普通用户和私有内存挂载。参考 [runtime.mount.in](../scripts/systemd/runtime.mount.in)，将 UID/GID 替换为实际值。内核和挂载必须支持 `noswap`；不满足时修复环境，不能删掉校验继续运行。
2. 将源码放到 `/opt/wxbridge`，运行用户应能读取源码并写入私有记忆目录。部署包要包含 `config.defaults.json`、`prompts/` 和脚本，不能只复制 Python 包。
3. 按 `examples/account.config.example.jsonc` 的注释创建标准 JSON 的 `config.json`，至少指定目标群与实际账号数据库目录。不要直接复制带注释文件作为 JSON，也不要把示例群名当作自己的群。
4. 从 `.env.example` 创建根目录 `.env`，在服务器本地填写 Key，权限 0600；不要粘贴到聊天、命令行参数或 Git。已有 `scripts/configure-deepseek.sh` 可隐藏输入，且拒绝覆盖已有凭据文件。
5. 以该普通用户启动微信会话，手机完成登录，再由管理员执行受控密钥扫描和账号/群检查。全新队列建立基线，不自动补答历史消息。

数据库密钥与 DeepSeek API Key 用途不同。数据库扫描需要相应本机进程读取权限，只对自己管理的已登录客户端操作；提取后验证密钥，存放在私有内存目录，由运行用户持有。持久服务的提权、密钥恢复和账号绑定见 [开机恢复](BOOT_RECOVERY.md)。

## 无物理桌面的会话

管理员先准备内存挂载。以下会话命令在运行用户下执行：

```bash
cd /opt/wxbridge
export WX_RUNTIME_DIR=/run/wxbridge-trial
bash scripts/start-headless.sh
```

脚本启动 Xvfb、D-Bus、AT-SPI、fluxbox 和微信，写入会话环境文件。它本身不创建 `tmpfs,noswap`，也不生成数据库密钥。未设置 `WX_RUNTIME_DIR` 时的临时会话路径不适用于这里的生产队列约定。

需要登录界面时，使用 `login-vnc.sh` 并通过 SSH 转发到 `127.0.0.1:5900`；完整命令见 [开机恢复](BOOT_RECOVERY.md)。VNC 只监听本机，完成手机认证后关闭。

## 检查与监听

进入同一普通账号、同一源码目录，加载微信会话环境：

```bash
cd /opt/wxbridge
set -a
source /run/wxbridge-trial/env
set +a
python3 -m wxbridge env
python3 -m wxbridge check
```

`check` 读取数据库并核对目标群，不显示聊天正文。数据库查询只能由一个所有者执行，监听运行时优先查看只读状态；需要刷新数据库的诊断先停止监听。

正式发送：

```bash
python3 -m wxbridge run --send
```

`run --dry-run` 不发送但仍可能调用模型；`--echo` 以固定回复替代模型，用于受控链路验证。已有 `scripts/run-live.py` 转入相同 CLI/runtime，并显式启用发送。纯离线回归用 `python3 scripts/test-offline.py`。

查看队列不启动第二个监听，也不刷新解密数据库：

```bash
python3 -m wxbridge queue-status
```

记忆维护命令见 [人物记忆](MEMORY.md) 和 [语境学习](LEARNING.md)。修改类维护先停止监听并取得同一锁。

SSH交互状态面板使用 `python3 -m wxbridge status`；root可在源码目录执行 `runuser -u wxbridge -- python3 -m wxbridge status`。支持五个页面、滚动、暂停和手动刷新；退出只结束查看。非交互会话用 `status --once`。完整按键和状态含义见[状态面板](STATUS.md)。

## 更新与恢复

先跑离线检查、备份旧代码，再停止 `wxbridge-bot.service` 并记录接收游标、账号、群及未决发送状态。成套更新源码、默认 JSON、脚本和测试；保留服务器实际 `config.json`、`.env`、人格、记忆、运行队列及接收游标。不要用全目录清空或覆盖部署破坏这些数据。

只重启监听可以保留微信登录和内存队列；整机重启会丢失内存中的未完成问题/回复，持久检查点只保留身份和游标，不存正文。账号或群不匹配时停止排查，不能直接删除检查点绕过。详细服务状态与恢复步骤见 [开机恢复](BOOT_RECOVERY.md)。

上线后先核对监听、队列和安全错误码，再由群成员发一个新真实 @ 验证手机显示与数据库确认。遇到结果不明不要盲目补发。已知限制与排障见 [常见问题](TROUBLESHOOTING.md)。

## 可核对的发布包

先完成离线测试、仓库检查和提交，再构建包；输出放仓库外：

```bash
python3 scripts/test-offline.py
python3 scripts/check-repository.py --history
python3 scripts/release.py build ../wxbridge-release.zip
python3 scripts/release.py verify ../wxbridge-release.zip
```

构建只取干净 Git 工作区中的 HEAD 已提交文件，不读取服务器、不包含忽略的运行数据。清单保存完整提交号、每个文件的 SHA-256 与 Git 权限；拒绝私密路径、凭据特征、路径穿越和已有输出文件。`verify` 是传输完整性检查，不是发行签名，包仍应来自自己的受信渠道。

部署顺序固定为：服务器暂存并验证包 → 用服务器解释器在暂存目录跑离线测试 → 停止监听（保留微信会话和内存挂载）→ 记录只读队列状态 → 备份当前代码 → 更新清单中的代码及默认 JSON → 启动监听并核对状态。不要把整个包直接解压覆盖运行目录。

更新时保留服务器 `config.json`、`.env`、`memory/`、`prompts/system.md`、`prompts/memory.md` 和全部 `/run/wxbridge-trial` 数据。包中的提示词是参考版本；明确要更新人格或整理规则时才单独合并。携带 `release-manifest.json`，`live-status.json` 就能报告运行提交号；有效配置另有哈希便于核对。没有清单的开发目录回退读取 Git 提交号。

部署脚本默认拒绝删除旧发布文件、默认保留服务器提示词。确认目录迁移后，可为每个旧路径传入一次 `--allow-removed 路径`，清单必须与实际移除项完全一致；旧文件进入同一私有备份，失败可恢复。用户明确要求同步当前人格时，使用 `--update-prompts` 将版本化提示词一并备份和更新。两项都不改变密钥、记忆与队列的保护。

本次队列迁移使用 SQLite 生成列与索引，旧版二列写入仍兼容。回滚时停止监听、恢复备份代码和相匹配的默认 JSON，保留当前队列；不要用旧队列快照覆盖已确认的新发送记录。内存队列的备份也必须留在同一私有内存挂载中，不能复制聊天任务到普通磁盘。发布工具负责构建与验证，不自动停止服务、覆盖生产文件或推送 GitHub。

## 独立实例目录

本版根 config.json 只接受账号级字段。每个群的配置和提示词单独放在应用 instance/<id>/ 下；启动时发现并校验 enabled=true 的实例，不再读取根 instances 或 overrides。首次迁移先填写稳定群 ID、模型和账号发送允许清单，未配置群保持停用。

升级须保留整个 instance/ 目录及 provider_env 指定的凭据文件；默认源码配置为 src/provider/.env。发布检查会核对应用内实例文件和默认 provider 配置的哈希，代码更新不改写它们。队列及游标仍在运行目录 instances/<id>/ 下，实例 ID 变更属于新运行身份，不能用改目录名来迁移未决发送。

本地实际实例不进入发布包，公开模板位于 examples/instance/。修改配置后重启监听；本次代码调整未操作服务器。
