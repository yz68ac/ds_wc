# wxbridge 代码地图

当前目录按四个职责组织，根目录只保留命令入口、版本和配置。这次重组仅在 Windows 本地完成，Linux 仍运行此前版本。

```text
wxbridge/
├── __init__.py            包说明与版本
├── __main__.py            python -m wxbridge 入口
├── cli.py                 命令参数与分发
├── config.py              默认配置、根目录 .env、路径解析
├── app/                   应用调度
│   ├── runtime.py         启动规则、单实例锁、退出清理
│   ├── bot.py             接收、生成调度、冷却、发送确认、状态
│   ├── workers.py         可终止的 API/UI 工作进程
│   └── diagnostics.py     环境、数据库和界面诊断
├── ai/                    生成回答
│   ├── client.py          DeepSeek HTTP 请求、重试、最终答案提取
│   ├── prompts.py         人格读取与文本提示词构造
│   ├── context.py         十分钟历史窗口与去重
│   └── images.py          本地图片内存解密、校验、组装多模态请求
├── wechat/                Linux 原生微信适配
│   ├── store.py           数据库查询、联系人与图片附件映射
│   ├── crypto.py          SQLCipher 页与 WAL 解密
│   ├── keyscan.py         从本机微信进程提取并验证数据库密钥
│   ├── references.py      引用关系、图片标识与模型可见字段
│   ├── messages.py        文本触发兼容与回复分片
│   └── ui.py              AT-SPI/X11 界面控制和发送保护
└── storage/               运行状态存储
    ├── journal.py         SQLite 队列、游标、意图与恢复
    └── memoryfs.py        私有 tmpfs,noswap 检查
```

建议先读 [cli.py](cli.py)，再读 [app/runtime.py](app/runtime.py) 和 [app/bot.py](app/bot.py)。想调整生成内容，查看 [ai/prompts.py](ai/prompts.py)、[ai/client.py](ai/client.py)；人格正文仍在上一级 [prompts/system.md](../prompts/system.md)。完整流程见 [代码导读](../docs/CODE_GUIDE.md)。

主进程负责所有微信数据库查询；工作进程只调用模型或操作界面。`ai/images.py` 接收主进程给出的本地文件描述，不创建 `WeChatStore`，不保存图片明文。队列、冷却、真实 @、图片引用及确认策略保持原行为。

`.env`、`config.json`、`prompts/system.md` 仍在项目根目录对应位置；移动 Python 文件没有移动这些配置。启动命令仍为 `python3 -m wxbridge`，服务入口仍为 `scripts/run-live.py`。
