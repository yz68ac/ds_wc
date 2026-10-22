# wxbridge 代码地图

当前目录按四个职责组织，根目录只保留命令入口、版本和配置。Linux 环境准备见 [部署说明](../docs/DEPLOYMENT.md) 为准。

```text
wxbridge/
├── __init__.py            包说明与版本
├── __main__.py            python -m wxbridge 入口
├── cli.py                 命令参数与分发
├── config.py              加载上层 JSON、根目录 .env、路径解析与校验
├── app/                   应用调度
│   ├── runtime.py         启动规则、单实例锁、退出清理
│   ├── bot.py             接收、生成与后台调度、状态
│   ├── delivery.py        发送提交、冷却、UI结果和发出记录确认
│   ├── conversation.py    确认后的会话变化纯函数
│   ├── task_policy.py     生成错误分类，不依赖具体传输
│   ├── metrics.py         有界耗时和API用量统计
│   ├── build_info.py      运行提交号和有效配置哈希
│   ├── proactive.py       主动接话判断、前台优先和发送前复核
│   ├── learning.py        活跃闲聊学习、任务登记和去重
│   ├── workers.py         可终止的 API/UI 工作进程
│   └── diagnostics.py     环境、数据库和界面诊断
├── ai/                    生成回答
│   ├── client.py          按角色调用HTTP、结构化结果、安全错误
│   ├── roles.py           独立模型角色与旧配置兼容解析
│   ├── prompts.py         人格读取与文本提示词构造
│   ├── context.py         可配置历史窗口与去重，默认五分钟
│   ├── memory.py          成员标签、带来源快照和后台结构化整理请求
│   ├── avatars.py         独立头像视觉分类、有限枚举校验；不推测人格
│   ├── search.py          按需搜索/抓取、工具循环与真实来源
│   ├── proactive.py       主动发言/沉默的结构化模型决策
│   ├── emoticons.py       微信表情名称和人工维护的语义参考
│   ├── output.py          简短群聊输出规则与纯文本格式兜底
│   ├── wxgf.py            选择彩色HEVC流，受控子进程内存转PNG
│   └── images.py          本地图片内存解密、校验、组装多模态请求
├── wechat/                Linux 原生微信适配
│   ├── store.py           数据库查询、联系人与图片附件映射
│   ├── crypto.py          SQLCipher 页与 WAL 解密
│   ├── keyscan.py         从本机微信进程提取并验证数据库密钥
│   ├── references.py      引用关系、图片标识与模型可见字段
│   ├── pats.py            拍一拍解析；实时同步尚未稳定验收
│   ├── messages.py        文本触发兼容与回复分片
│   └── ui.py              AT-SPI/X11 界面控制和发送保护
└── storage/               运行状态存储
    ├── journal.py         SQLite 队列、游标、意图与恢复
    ├── person_memory.py   人物记忆、来源校验、遗忘、分数和原子写入
    ├── expression_memory.py 语境表达证据、成熟、反例、遗忘和提示词注入
    └── memoryfs.py        私有 tmpfs,noswap 检查
```

建议先读 [cli.py](cli.py)，再读 [app/runtime.py](app/runtime.py) 和 [app/bot.py](app/bot.py)。想调整生成内容，查看 [ai/prompts.py](ai/prompts.py)、[ai/client.py](ai/client.py)；人格正文仍在上一级 [prompts/system.md](../prompts/system.md)。完整流程见 [代码导读](../docs/CODE_GUIDE.md)。

主进程负责所有微信数据库查询；工作进程只调用模型或操作界面。`ai/images.py` 接收主进程给出的本地文件描述，不创建 `WeChatStore`，不保存图片明文。队列、冷却、真实 @、图片引用及确认策略保持原行为。

需要联网时，API 工作进程发送内部进度事件，默认只向群里发送最终回答。显式开启 `search.feedback_enabled` 才把一次性等待提示纳入同一确认流程；关闭后不补发未提交提示，已有意图仍核对发出记录。搜索详情见 [SEARCH.md](../docs/SEARCH.md)。

全部默认值已从 `config.py` 提取到上一级 `config.defaults.json`，自定义 `config.json` 覆盖默认值。`.env`、`config.json`、`prompts/system.md` 仍在项目根目录对应位置。启动命令仍为 `python3 -m wxbridge`，服务入口仍为 `scripts/run-live.py`。后台整理提示词为 `prompts/memory.md`；更新时成套携带默认 JSON 和提示词，不覆盖服务器实际配置与私密数据。
