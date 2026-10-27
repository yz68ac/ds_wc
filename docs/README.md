# 文档导航

项目入口见 [README](../README.md)。先理解一次收发，再按修改目标进入对应模块；历史逆向过程与当前运行要求分开阅读。

## 先看哪里

| 想做什么 | 阅读顺序 |
| --- | --- |
| 参与开发 | [贡献流程](../CONTRIBUTING.md) → [代码导读](CODE_GUIDE.md) → [包内地图](../wxbridge/README.md) |
| 理解当前架构 | [架构与故障边界](ARCHITECTURE.md) → [上下文选择](CONTEXT.md) |
| 配置与部署 | [配置](CONFIG.md) → [Linux 部署](DEPLOYMENT.md) → [开机恢复](BOOT_RECOVERY.md) |
| 查看运行情况 | [SSH 状态](STATUS.md) → [常见问题](TROUBLESHOOTING.md) |
| 理解后台发送与注入 | [native 代码地图](../wxbridge/native/README.md) → [发送接口](NATIVE_API.md) → [分析总结图](NATIVE_ANALYSIS.md) |
| 找运维或验证脚本 | [脚本分类与执行条件](../scripts/README.md) |

## 按功能查阅

| 功能 | 文档 | 主要代码 |
| --- | --- | --- |
| 人格、短答与纯文本 | [代码导读](CODE_GUIDE.md) | `prompts/system.md`、`ai/prompts.py`、`ai/output.py` |
| 五分钟历史与人物记忆检索 | [上下文](CONTEXT.md) | `ai/context.py`、`ai/identity.py`、`storage/memory_selection.py` |
| 引用文字与图片 | [引用](REFERENCES.md)、[图片](VISION.md) | `wechat/references.py`、`ai/images.py`、`ai/wxgf.py` |
| 联网搜索 | [搜索](SEARCH.md) | `ai/search.py` |
| 微信表情 | [表情语义](EMOTICONS.md) | `ai/emoticons.py`、`prompts/wechat_emoticons.json` |
| 人物记忆与关系 | [记忆](MEMORY.md)、[关系](RELATIONSHIPS.md) | `storage/person_memory.py`、`storage/relationships.py` |
| 语境与表达学习 | [语境学习](LEARNING.md) | `app/learning.py`、`storage/expression_memory.py` |
| 关键词与主动参与 | [主动接话](PROACTIVE.md) | `app/proactive.py`、`ai/proactive.py` |
| 接收、排队与发出确认 | [架构](ARCHITECTURE.md)、[发送接口](NATIVE_API.md) | `app/bot.py`、`app/delivery.py`、`storage/journal.py` |

表中代码路径相对于 `wxbridge/`；人格文件相对于仓库根目录。

## 当前验证边界

后台文本发送已验证可指定收件人，不依赖当前聊天窗口；微信启动、登录和开机恢复仍保留 Xvfb / X11 / AT-SPI 环境。图片理解是读取已绑定的本地图片后交给模型，发送图片、引用回复和收藏表情尚未封装为原生接口。拍一拍的实时同步仍未稳定验收。

仓库不包含真实配置、登录态、数据库或记忆。提交与发布前查看 [安全说明](../SECURITY.md)。源码目录之外的研究、部署记录和发布包属于本机资料，不是 GitHub 协作所需文件。
