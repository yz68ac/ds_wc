# wechat_linux 代码地图

现行接口、支持程度和示例见 [状态与调用方式](../../docs/WECHAT_LINUX.md)，逆向与实验过程见 [历史参考](../../docs/reference/README.md)。

| 目录 / 文件 | 职责 |
| --- | --- |
| session.py | 显式账号会话，组合 reader、sender 和 downloader |
| models.py / errors.py | 消息、图片、提交回执和异常 |
| storage/ | SQLCipher / WAL 解密、账号、联系人、会话和消息读取 |
| messages/ | 引用和拍一拍的协议解析 |
| native/ | helper 通信、文字/图片提交、可选下载、Frida 资产和版本档案 |
| media/ | 本地图片处理、DAT 解密、WXGF 解码 |
| desktop/ | X11 / AT-SPI 兼容组件 |
| desktop/login.py | 已有微信进程的登录状态、按钮动作和二维码读取；生命周期由调用方管理 |
| runtime/ | 通用私有目录与文件锁 |
| analysis/ | ELF、指纹、候选地址和离线反汇编 |
| profiles.py | 声明能力和版本，不执行在线健康检查 |

业务队列、本地 API 服务器、发送确认、收件人策略和 tmpfs 挂载策略位于 wxbridge。库不读取模型配置或机器人实例。

离线入口：python -m wechat_linux analysis profiles。
