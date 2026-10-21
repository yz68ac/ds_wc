# Linux 重启恢复与本轮关机说明

2026-10-21，Asia/Singapore。适用于当前 Debian 主机 `192.168.1.212`（SSH 别名 `hotman`），代码 `/opt/wxbridge`，用户 `wxbridge`。没有更改 Minecraft 原有开机启动策略。

## 下次开机的流程

1. `wxbridge-session.service` 拉起依赖的私有内存盘和运行目录，再启动 Xvfb、D-Bus、AT-SPI、窗口管理器和微信。微信主进程退出时由 systemd 有界重试。
2. `wxbridge-recover.timer` 在开机30秒后开始检查，未准备好时每60秒重试。
3. 微信登录且打开本账号数据库后，协调器重新提取并校验 SQLCipher 密钥，只保存在 `/run/wxbridge-trial` 的 `tmpfs,noswap` 中。根权限扫描器的代码独立放在 root 所有的 `/usr/local/lib/wxbridge-recovery`。
4. 用普通账号核对微信账号、目标群、已有内存队列及持久游标；不匹配则等待修复，不启动错误账号的监听。主界面需能识别目标群。
5. 校验成功后启动 `wxbridge-bot.service`，继续使用当前模型、人格、五分钟上下文、记忆、队列与发送确认。

正式服务定义位于 `/etc/systemd/system`，重启后仍存在。原先的 `wxbridge-headless-trial` 和 `wxbridge-bot-live` 为临时服务，本次关机前继续使用它们，下一次开机改用新服务名；不会同时启动两套监听。

## 明早检查

```powershell
ssh hotman "systemctl show wxbridge-session wxbridge-bot wxbridge-recover.timer -p Id -p ActiveState -p SubState"
ssh hotman "python3 /usr/local/libexec/wxbridge-boot.py status"
```

`phase=listening` 表示监听已运行。`waiting_for_login_or_repair` 需要查看安全原因码。微信可能要求手机扫码或确认登录；此类认证不能由脚本保证自动完成。

需要查看登录界面时，临时开启仅本机监听的 VNC，并建立 SSH 隧道：

```powershell
ssh -L 5900:127.0.0.1:5900 hotman "runuser -u wxbridge -- env WX_RUNTIME_DIR=/run/wxbridge-trial bash /opt/wxbridge/scripts/login-vnc.sh"
```

用已有 VNC 客户端连接 `127.0.0.1:5900`，手机完成登录。登录后等最多一分钟，恢复定时器会再检查，也可手动执行：

```powershell
ssh hotman "systemctl start wxbridge-recover.service"
```

VNC 仅临时使用，完成后 Ctrl+C 关闭。未安装新的 Windows VNC 客户端；也可明天在当前对话请求查看登录二维码。若提示目标群不可见，先在微信主界面打开目标群。若账号/群或游标不匹配，不要直接删除检查点，应核实当前登录账号和配置。

## 状态和数据

- `/var/lib/wxbridge/recovery/cursor.json`：0600，只含账号、群、接收游标和未完成数量；不含问题、回复、图片或密钥。
- `/run/wxbridge-trial/boot-status.json`：启动阶段和安全错误码。
- `/run/wxbridge-trial/live-status.json`：监听与队列状态。
- `/etc/wxbridge-boot.json`：root 0600，绑定当前账号的数据库路径，不含 API Key。
- `/opt/wxbridge/.env`、人格、配置和 `memory/` 保留原位置和内容。

普通进程重启仍恢复原内存队列；整机重启会清空内存中的未完成问题和回复，不把正文转存到磁盘。接收游标继续保留，避免重放旧回答；离线期间的旧 @ 仍按既有90秒接收时效过滤，不保证补答。拍一拍的实时同步尚未跑通，保持此前搁置状态，不属于本轮恢复验证。

## Minecraft 和自动关机

本轮检查时 Minecraft 游戏容器已经是停止状态，只有 frpc 运行；没有替你启动游戏服或修改其启动配置。明早需要时执行 `mc start`，已有 `mc-deploy-recover.service` 的开机设置保留。

按本轮授权执行了 `mc idle off`，自动关机已关闭，而且重启后仍关闭。想恢复原来的无人10分钟关机策略，执行：

```powershell
ssh hotman "mc idle on"
ssh hotman "mc idle status"
```

## 验证范围和备份

Windows 205项离线测试通过（3项Linux专用跳过），Linux隔离源码及实际安装组合全部205项通过，另含解析、加密、WAL脚本。systemd配置验证通过；当前登录会话下重新提取密钥、账号与群核对、游标匹配通过，未调用模型或发送测试群消息。

安装当晚没有执行整机重启测试；后续真实开机结果见下一节，免手机认证仍未得到验证。初始备份 `/var/backups/wxbridge/boot-20261021-72ab76e0`；脱敏部署记录位于 Windows 上层 `archives/deployments/20261021-boot-recovery`。

### 2026-10-21 早晨实际开机验证

服务器实际重启后，挂载、图形会话和恢复定时器正常；微信停在保存账号的 `Log In`，点击后需要手机确认。完成认证后已实证自动重新提取密钥并恢复游标644及人物记忆。微信登录和手机认证仍需人工参与。

修复一处冷启动误判：主界面目标群嵌套超过14层，原查找深度不足；现扫描35层并排除隐藏旧窗口控件。首轮修复207项测试通过，备份 `/var/backups/wxbridge/coldstart-20261021-bea195de`。真实新@消息646已对应本人发出记录647，用户确认手机收到，重启后的完整收发已验证。

随后引用图片的消息651已读取到图片并发出搜索提示652，但最终回答在输入正文之前因目标群未选中而暂停。持锁检查未发现最终回复发出记录，编辑器为空；重新选群后状态恢复。首次点击为何未选中仍未确认，不将其归因于微信风控或图片API。加固选择流程：目标群已选中时不重复点击，只查询可见的深层列表；点击后有界等待选中状态，并最多重选一次；搜索结果不明时不按回车。错误群、不同草稿和数据库发送确认保护保留。

Windows212项离线检查通过（3项Linux专用跳过），Linux实际安装组合212项全部通过，另含解析、加密和WAL脚本。核对未发记录和输入前错误证据后恢复原任务，最终两片分别确认发出653、654；队列0、阻塞为空、监听正常，人物记忆修订11。此轮备份 `/var/backups/wxbridge/selection-20261021-f9bbd5a9`；服务器实际配置、密钥和人格未覆盖。
