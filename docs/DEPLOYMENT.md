# 运行说明

此文说明现有部署及完整使用流程。2026-10-20 22:02（UTC+08）已上线人物记忆及恢复保护，当前包含默认配置 JSON 提取；Windows 后续编辑仍需另行部署才生效。

本文后面按时间记录的平铺模块名属于当时部署。当前分层结构见 [代码导读](CODE_GUIDE.md)，目录重组的最初本地备份见 [整理记录](../../archives/refactors/20261020-package-layout/README.md)。启动命令与配置位置保持不变。用户本轮已授权将分层代码、最新人格和联网搜索一起同步，验证结果、实际备份路径与游标见 [搜索部署记录](../../archives/deployments/20261020-websearch/README.md)。

默认值已提取到 `config.defaults.json` 并随本轮记忆功能部署。以后部署 `wxbridge/config.py` 时必须同时携带源码根目录的默认 JSON；实际服务器 `config.json`、`.env` 和人格继续保留，不用默认配置覆盖它们。

## 22:02 人物记忆上线

实现前 153 项既有离线测试通过，基线提交 `eb5ac6a`。本轮 Windows 176 项通过（3 项 Linux 专用测试跳过），Linux 176 项全部通过，另有解析、加密与 WAL 脚本；以服务账号回归实际安装组合也通过。

后台采用 deepseek-flash、关闭思考，前台仍为 high。合成实际 API 验证完成两位成员的偏好提取、来源校验、私有写入和下一轮提示词注入，不读取真实聊天或发送群消息。真实「来打灰」两轮 @ 后，用户确认第二轮记住了偏好；后台修订 2、水位 614、涉及 2 位成员、3 条摘要，回复队列和后台队列均为空、无错误。未预导入全部群历史。

首次备份 `/var/backups/wxbridge/memory-20261020-215144`，最终恢复保护补丁备份 `/var/backups/wxbridge/memory-20261020-220207`。最终切换前后游标均为 615，保留已有记忆；config.json、.env、prompts/system.md 的哈希未变。监听 active/running，记忆目录 `/opt/wxbridge/memory` 为 0700，memory.json 与 additional_prompt.md 为 0600，属主 wxbridge。微信会话未重启。

恢复保护覆盖 JSON 写入失败不提前推进内存水位、投影失败不重复计分、旧任务先按遗忘水位跳过；额外验证晚同步的较早消息拥有更大 local_id 时也不重复计分。记忆命令及边界见 [MEMORY.md](MEMORY.md)；代码和脱敏部署证据在 [本轮记录](../../archives/deployments/20261020-memory/README.md)。

## 现有部署

| 项目 | 位置 |
| --- | --- |
| Linux | `192.168.1.212`，SSH 别名 `hotman`；Debian 13.7 amd64 |
| 微信 | `/opt/wechat/wechat`，4.1.13.26 |
| 普通运行用户 | `wxbridge`，主目录 `/var/lib/wxbridge` |
| 代码与配置 | `/opt/wxbridge`、`config.json`、`.env`；凭据文件 0600 |
| 内存运行数据 | `/run/wxbridge-trial`，独立 1 GiB `tmpfs,noswap`，0700 |
| 微信会话服务 | `wxbridge-headless-trial.service` |
| 监听服务 | `wxbridge-bot-live.service`，入口 `scripts/run-live.py` |

## 完整操作顺序

1. **环境与配置**：准备原生微信、Xvfb、D-Bus、AT-SPI/PyGObject 和 Python 依赖。由管理员建立专用用户及私有内存挂载。复制示例配置，填目标群和实际数据库目录；API Key 通过 `configure-deepseek.sh` 在 SSH 终端隐藏输入，已有文件不会被覆盖。
2. **启动微信会话**：使用 `start-headless.sh`；现有部署由上述微信会话服务托管。脚本创建 Xauthority，写 `env`，再启动图形会话和微信。单纯启动这个脚本不会创建内存挂载或数据库密钥。
3. **登录**：需要时临时开启 VNC，SSH 转发本机端口后查看界面，手机扫码确认。`login-vnc.sh` 会加载当前会话的环境与 Xauthority。登录后关闭 VNC，保留微信会话。
4. **准备消息读取**：在相应权限下调用 `scan-keys`，确认密钥文件归运行用户所有且为 0600；运行目录必须是该用户所有的私有 `tmpfs,noswap`。`check` 确认目标群和最新 local_id，不打印正文。
5. **验证并监听**：新版本用 `run --dry-run` 禁止发送，用 `--echo` 可跳过模型；正式 `run --send` 显式启用发送。dry-run 仍可能调用模型，纯离线验证应使用测试脚本。既有服务入口 `run-live.py` 转入相同流程并传 `--send`。
6. **检查与停止**：查看 systemd 状态和内存中的 `live-status.json`；以实际进程/服务状态为准。停止监听可保留微信登录，停止微信会话会同时结束依赖它的监听。

CLI 必须和微信使用同一普通账号及 D-Bus/X11 会话。进入该账号后先加载运行目录中的环境文件：

```bash
cd /opt/wxbridge
set -a
source /run/wxbridge-trial/env
set +a
python3 -m wxbridge env
python3 -m wxbridge check
```

只读查看队列（不需启动第二个监听实例，不刷新微信解密数据库）：

```bash
python3 -m wxbridge queue-status
```

生产与 dry-run 均有单实例锁；同一解密目录不能同时运行第二个监听。`check` 会读数据库，运行中排障优先查看 `queue-status` 与状态文件。

## 从 Windows 管理现有服务

查看服务状态，不显示聊天正文：

```powershell
ssh hotman "systemctl show wxbridge-headless-trial wxbridge-bot-live -p Id -p ActiveState -p SubState -p MainPID"
```

只停止监听：

```powershell
ssh hotman "systemctl stop wxbridge-bot-live"
```

停止微信和它所依赖的监听：

```powershell
ssh hotman "systemctl stop wxbridge-headless-trial"
```

## 已验证范围与尚未自动化的部分

此前已验证数据库密钥 19/19、真实新消息读取、固定消息发出、DeepSeek API 和一次真实 @ 回复。此次 Windows 离线测试 59 项（Linux 锁测试在 Windows 跳过），Linux 59 项全部通过，另含分片/提示词与加密/WAL 脚本。

此次部署先测试再备份，旧代码位于 `/var/backups/wxbridge/queue-20261020-141145/source-before.tar.gz`。服务新 PID 为 22830（首次上线时），`KillMode=control-group`；队列与锁均为 wxbridge 所有的 0600，位于已验证的 `tmpfs,noswap`。

记录并从旧游标 203 迁移；41 字草稿已按授权清空至 0 字，未补发。原 `config.json`、`.env` 和已有人格文件校验不变。已有内联人格继续按原配置生效；部署不会强制改成人格文件。

真实测试曾复现按钮动作返回成功但 33 字草稿未清空，队列按设计暂停。继续定位确认按钮只提供 `SetFocus`，修复动作名称识别及原生窗口命中核验后，14:27:27、14:27:41 分别确认两条原定回复的发出记录 226、227。队列已清空、草稿 0 字，恢复正常监听。用户决定关闭连续 @ 合并，服务器保持 `queue.debounce_seconds=0`，每次 @ 独立排队。

14:36 继续修复鼠标已位于目标坐标时 `mousemove --sync` 等待直至超时的问题；改为显式核验坐标和活动窗口。异常现在记录命令阶段和是否尚未提交；只有明确未提交的临时错误才有界重试，未知点击仍暂停确认。

最终 Windows 75 项回归通过（Linux 专用锁测试跳过），Linux 75 项全部通过，另有分片/提示词及加密/WAL 脚本。14:36:20、14:36:34 两条最新测试回复确认记录 246、247，用户手机确认收到；14:37 新发的一次独立 @ 也完整通过，确认记录 249。实际按钮命中原生主窗口、每次草稿均清空至 0。

最终监听 PID 26437，队列 0，无阻塞，合并 0、生成并发 2。最后代码备份 `/var/backups/wxbridge/queue-20261020-143613/`；配置、密钥及人格设置校验不变。服务保持运行，最后的说明文档更新不重启服务。部署证据与脱敏脚本在 Windows 外层 `archives/deployments/20261020-queue/`。

现有服务和挂载为临时部署，没有开机恢复；重启后需重新准备挂载、会话和密钥。长期重连、客户端升级兼容尚未验证。归档中的固定哈希部署脚本只记录当时过程，不能当作当前自动安装器重跑。

应用日志默认不保存正文。`a11y` 等显式排障可能显示私密内容；源码与归档材料应按用途分别查看。

## 16:14 引用回复支持

已上线 `references.py`：支持引用消息后真实 @ 提问，将当前问题与引用正文、作者和原时间分别提供给模型。明确引用可早于十分钟窗口；仍按当前提问时间取普通上下文。只展开当前提问明确引用的快照，不访问其他群、网页或媒体。图片/语音等暂无识别，明确标记不可读。

该版本 Windows 90 项回归通过（Linux 专用锁测试跳过），Linux 90 项全部通过，另含算法脚本。备份 `/var/backups/wxbridge/queue-20261020-161334/`；配置、密钥和人格设置校验不变，合并保持 0。

用户真实引用测试：消息 295 识别为 `quoted_text`，带 8 字引用正文生成；16:14:34 确认发出记录 296，草稿清空、队列归零。用户确认实际回复正确理解了引用。当前入口、收发链路和模型均保留，增加 `recent_intake` 过滤原因与引用元数据，不保存正文。

使用示例和边界见 [引用消息说明](REFERENCES.md)，完整脱敏证据见 Windows `archives/deployments/20261020-queue/REFERENCE-RESULTS.md`。

## 联网搜索、分层目录与新人格同步

新增 `ai/search.py`，保持 `deepseek-flash` 和现有图片能力，使用同一 Key 调用 Search/Fetch。每个问题按需搜索，最多一次口语提示；提示和最终回复分别以数据库发出记录确认并遵守冷却，支持进程重启恢复。Search/Fetch 失败会说明无法核实，日志只存数量和错误码。

本轮同步包含之前仅在本地修改的分层目录和更有主见的人格。部署脚本先执行 Linux 离线测试，再用合成问题验证实际搜索工具链；完成备份后才停止监听、切换代码及人格，移除已备份的旧平铺模块。服务器 `config.json`、`.env`、微信会话、接收游标和队列保留。具体结果以本轮部署记录为准；历史固定哈希/一次性恢复脚本不直接重跑。
