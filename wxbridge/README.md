# wxbridge 代码地图

当前目录按五个职责组织，根目录保留命令入口、版本和配置。Linux 环境准备以 [部署说明](../docs/DEPLOYMENT.md) 为准；文档分类见 [导航](../docs/README.md)。

```text
wxbridge/
├── __init__.py            包说明与版本
├── __main__.py            python -m wxbridge 入口
├── cli.py                 命令参数与分发
├── config.py              加载上层 JSON、根目录 .env、路径解析与校验
├── app/                   应用调度
│   ├── runtime.py         启动规则、单实例锁、退出清理
│   ├── bot.py             接收、生成与后台调度、状态
│   ├── delivery.py        发送提交、冷却、原生/UI结果和发出记录确认
│   ├── conversation.py    确认后的会话变化纯函数
│   ├── task_policy.py     生成错误分类，不依赖具体传输
│   ├── metrics.py         有界耗时和API用量统计
│   ├── monitor.py         只读状态/服务/安全事件采集及文本页面
│   ├── terminal.py        SSH curses交互、切页、滚动和刷新
│   ├── build_info.py      运行提交号和有效配置哈希
│   ├── proactive.py       主动接话判断、前台优先和发送前复核
│   ├── learning.py        活跃闲聊学习、任务登记和去重
│   ├── workers.py         可终止的 API、原生发送及UI工作进程
│   └── diagnostics.py     环境、数据库和界面诊断
├── ai/                    生成回答
│   ├── client.py          按角色调用HTTP、结构化结果、安全错误
│   ├── roles.py           独立模型角色与旧配置兼容解析
│   ├── prompts.py         人格读取与文本提示词构造
│   ├── context.py         可配置历史窗口与去重，默认五分钟
│   ├── identity.py        稳定成员编号、BOT标记与引用作者一致映射
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
├── native/                指定收件人的后台文本发送
│   ├── command.py         native-api CLI入口
│   ├── client.py          外部Python客户端
│   ├── api.py             主进程私有接口、允许清单与请求去重
│   ├── protocol.py        有界IPC、错误分类和UTF-8分片
│   ├── sender.py          发送工作进程到helper的适配
│   ├── helper.py          root隔离进程、版本校验与短时Frida连接
│   ├── agent.js           微信进程内的后台服务调用
│   └── profile.json       固定版本hash和函数指纹
└── storage/               运行状态存储
    ├── journal.py         SQLite 队列、游标、意图与恢复
    ├── person_memory.py   人物记忆、来源校验、遗忘、分数和原子写入
    ├── memory_selection.py 按当前话题检索记忆，避免旧话题干扰
    ├── relationships.py   好感、信任、熟悉度与短期情绪校验
    ├── expression_memory.py 语境表达证据、成熟、反例、遗忘和提示词注入
    └── memoryfs.py        私有 tmpfs,noswap 检查
```

建议先读 [cli.py](cli.py)，再读 [app/runtime.py](app/runtime.py) 和 [app/bot.py](app/bot.py)。想调整生成内容，查看 [ai/prompts.py](ai/prompts.py)、[ai/client.py](ai/client.py)；人格正文仍在上一级 [prompts/system.md](../prompts/system.md)。完整流程见 [代码导读](../docs/CODE_GUIDE.md)。

主进程负责所有微信数据库查询；工作进程只调用模型或执行发送。`ui.backend=native_service` 经 `native/sender.py → root helper → 微信进程内 agent.js` 提交，不读取输入框或选择会话；桌面后端仍由 `wechat/ui.py` 执行。两者均由主进程核对新增发出记录。进程、权限和注入边界见 [native 地图](native/README.md)。

`ai/images.py` 接收主进程给出的本地文件描述，不创建 `WeChatStore`，不保存图片明文。当前话题的记忆选择由 `storage/memory_selection.py` 处理，不修改权威人物记录；成员和引用作者由 `ai/identity.py` 统一编号。

需要联网时，API 工作进程发送内部进度事件，默认只向群里发送最终回答。显式开启 `search.feedback_enabled` 才把一次性等待提示纳入同一确认流程；关闭后不补发未提交提示，已有意图仍核对发出记录。搜索详情见 [SEARCH.md](../docs/SEARCH.md)。

全部默认值已从 `config.py` 提取到上一级 `config.defaults.json`，自定义 `config.json` 覆盖默认值。`.env`、`config.json`、`prompts/system.md` 仍在项目根目录对应位置。启动命令仍为 `python3 -m wxbridge`，服务入口仍为 `scripts/run-live.py`。后台整理提示词为 `prompts/memory.md`；更新时成套携带默认 JSON 和提示词，不覆盖服务器实际配置与私密数据。
