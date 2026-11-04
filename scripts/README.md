# 脚本入口

从仓库根目录执行。普通模块运行需先将 src 加入 PYTHONPATH。

| 入口 | 用途 |
| --- | --- |
| check-repository.py | 只读检查私密文件、凭据模式与本地文档链接；--history 可检查 Git 历史 |
| test-offline.py | 按需运行合成数据回归 |
| update-wechat-adapter.py | 从独立仓库的干净版本标签导入固定 wheel |
| release.py | 从已提交代码生成发布包，校验清单 |
| boot-session.py | 启动并监督微信会话，同时持有启动登录子进程 |
| wechat-login.py | 后台推进一次启动登录；前台通过 SSH TTY 查看二维码，--status 只读检查 |
| [wxbridge.host.login](../src/wxbridge/host/login.py) | 登录运行策略位于应用包；wechat-login.py 仅作为命令入口 |
| boot-recovery.py | 登录、密钥、身份与游标核对及监听恢复 |
| start-headless.sh / stop-headless.sh | 启停专用 Xvfb 会话 |
| login-vnc.sh | 临时查看登录界面，配合 SSH 隧道 |
| install-native-helper.py | 安装隔离 root helper 与版本资产 |
| systemd/ | 服务与私有内存挂载模板 |
| diagnostics/ | 显式的真实数据或接口诊断 |

运行中先用 `wxbridge status --json` 或 `wxbridge debug queue-status` 查看安全状态元数据。诊断入口可能读取真实消息、调用 API 或更改状态，执行前查看参数和源码。

[当前微信接口](../docs/WECHAT_LINUX.md) · [模型调用](../docs/PROVIDER.md) · [部署与运维](../README.md)
