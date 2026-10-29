> 历史参考，归档于 2026-10-29。正文保留当时的研究、实验或运维结论；接口、路径、测试数量和状态可能已过时。当前接口以 [wechat_linux 状态](../WECHAT_LINUX.md) 和 [provider 用法](../PROVIDER.md) 为准。

# 开机恢复与服务维护

仓库包含经过现有环境验证的 systemd 模板和恢复协调器，不是通用的一键安装器。路径、账号、微信程序和数据库目录需要按目标机器核对。依赖和首次配置见 [部署说明](DEPLOYMENT.md)。

## 启动顺序

1. 私有内存挂载建立 `/run/wxbridge-trial`，`wxbridge-runtime.service` 校验挂载、属主和权限。
2. `wxbridge-session.service` 以普通用户启动 Xvfb、D-Bus、AT-SPI、窗口管理器和微信。
3. `wxbridge-recover.timer` 开机 30 秒后开始检查，未准备好时每 60 秒重试。
4. 微信登录且数据库打开后，管理员权限协调器重新提取并验证数据库密钥，只写私有内存目录；普通账号核对身份、目标群、队列和持久游标。
5. 校验成功后启动 `wxbridge-bot.service`。身份不匹配、密钥缺失或目标群未就绪时等待修复。

登录、扫码和手机认证仍需账号持有人参与；不能承诺无人值守自动通过微信认证。不要同时启动另一套旧监听服务。

## 安装模板的核对项

模板在 [scripts/systemd](../../scripts/systemd)，协调器在 [boot-recovery.py](../../scripts/boot-recovery.py)，会话入口在 [boot-session.py](../../scripts/boot-session.py)。它们约定：

- 用户 `wxbridge`，HOME `/var/lib/wxbridge`，源码 `/opt/wxbridge`，微信 `/opt/wechat/wechat`。
- `runtime.mount.in` 替换实际 `@UID@`、`@GID@`，目标挂载单元名应由 `systemd-escape --path --suffix=mount /run/wxbridge-trial` 得到，并与模板依赖一致。
- `/etc/wxbridge-boot.json` 为 root 所有的 0600 文件，`db_dir` 填已登录账号的实际 `db_storage` 路径，必须位于运行用户 HOME 中。
- 管理员入口复制为 `/usr/local/libexec/wxbridge-boot.py`；管理员导入的包副本放 `/usr/local/lib/wxbridge-recovery/wxbridge/`。这些代码及其父目录必须由 root 持有、普通运行用户不可写；不能让提权扫描器直接导入朋友可修改的源码目录。
- 包副本需要包含依赖的 Python 模块；默认 JSON、实际配置和普通用户校验仍从应用目录读取。更新时要同步检查管理员副本，不能混用不同版本。
- 正式单元安装到 `/etc/systemd/system` 后执行 `systemctl daemon-reload`，再按依赖顺序启动、验证并启用会话与恢复定时器。模板本身不会创建用户、安装微信或复制受保护代码。

建议先审阅这些文件并在隔离账号验证，再配置开机启用。部署前保存现有服务定义，不能覆盖机器上同名但用途不同的服务。

## 检查与重启

在服务器管理员终端执行：

```bash
systemctl show wxbridge-session wxbridge-bot wxbridge-recover.timer -p Id -p ActiveState -p SubState
python3 /usr/local/libexec/wxbridge-boot.py status
```

`phase=listening` 表示监听运行；`waiting_for_login_or_repair` 要结合安全原因码处理。

只重启机器人，保留微信会话和内存队列：

```bash
systemctl restart wxbridge-bot.service
```

维护时同时暂停恢复定时器，防止监听被自动拉起：

```bash
systemctl stop wxbridge-recover.timer wxbridge-recover.service wxbridge-bot.service
# 完成维护后恢复自动检查
systemctl start wxbridge-recover.timer wxbridge-recover.service
```

普通进程重启先核对未决发送意图，不能直接重发。要关闭整个微信会话，先停止恢复定时器和监听，再停止 `wxbridge-session.service`。

## 手机登录

以下是 SSH 模板，把 `admin@your-server` 替换为自己的管理员连接地址：

```bash
ssh -L 5900:127.0.0.1:5900 admin@your-server "sudo -u wxbridge env WX_RUNTIME_DIR=/run/wxbridge-trial bash /opt/wxbridge/scripts/login-vnc.sh"
```

用已有 VNC 客户端连接 `127.0.0.1:5900`，手机确认登录。隧道依赖远端 sudo 权限；需要口令时使用交互终端。不要将 VNC 暴露公网。完成后关闭临时 VNC，再等待恢复定时器或执行 `systemctl start wxbridge-recover.service`。

目标群在主界面不可见时先打开该群；账号、群或游标不匹配时先核实实际登录账号和配置，不删除身份校验数据绕过。

## 数据与断电边界

| 位置 | 内容 |
| --- | --- |
| `/var/lib/wxbridge/recovery/cursor.json` | 持久检查点，0600；身份、游标、未完成数量，不含问题/回复/图片/密钥 |
| `/run/wxbridge-trial/boot-status.json` | 启动阶段和安全原因码 |
| `/run/wxbridge-trial/live-status.json` | 监听与队列元数据 |
| `/run/wxbridge-trial/queue.sqlite3` | 内存队列，进程重启可恢复，主机重启清空 |
| `/opt/wxbridge/.env`、`config.json`、`prompts/`、`memory/` | 保留在应用目录的配置、人格和记忆 |

正常停止尽量先让队列完成，再停止监听、保存检查点和结束微信会话。整机关机前应确认关键数据已写入；不要依靠强制断电保存内存任务。恢复后仍对首次接收的消息执行时效检查，不承诺补答所有离线消息。
