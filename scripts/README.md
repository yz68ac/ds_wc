# 脚本入口

从仓库根目录执行。普通模块运行需先将 src 加入 PYTHONPATH；run-live.py 自行设置源码路径。

| 入口 | 用途 |
| --- | --- |
| check-repository.py | 只读检查私密文件、凭据模式与本地文档链接；--history 可检查 Git 历史 |
| test-offline.py | 按需运行合成数据回归 |
| smoke-wheel.py | 隔离安装 wheel 并检查打包导入 |
| release.py | 从已提交代码生成发布包，校验清单 |
| run-live.py | 进入 wxbridge CLI 并显式启用发送 |
| boot-session.py | 启动并监督微信会话，同时持有启动登录子进程 |
| wechat-login.py | 后台推进一次启动登录；前台通过 SSH TTY 查看二维码，--status 只读检查 |
| login_runtime.py | GUI 环境、登录操作锁、按进程去重和安全状态记录 |
| boot-recovery.py | 登录、密钥、身份与游标核对及监听恢复 |
| start-headless.sh / stop-headless.sh | 启停专用 Xvfb 会话 |
| login-vnc.sh | 临时查看登录界面，配合 SSH 隧道 |
| install-native-helper.py | 安装隔离 root helper 与版本资产 |
| systemd/ | 服务与私有内存挂载模板 |
| diagnostics/ | 显式的真实数据或接口诊断 |
| maintenance/ | 特定历史故障的修复入口 |

运行中先用 python -m wxbridge status --once 或 queue-status 查看安全状态元数据。诊断入口可能读取真实消息、调用 API 或更改状态，执行前查看参数和源码。

[当前微信接口](../docs/WECHAT_LINUX.md) · [模型调用](../docs/PROVIDER.md) · [历史开机恢复](../docs/reference/BOOT_RECOVERY.md) · [历史部署](../docs/reference/DEPLOYMENT.md)
