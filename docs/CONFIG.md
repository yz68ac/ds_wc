# 配置项说明

全部默认配置保存在源码根目录 [config.defaults.json](../config.defaults.json)，与 `wxbridge` 文件夹同级。[config.example.jsonc](../config.example.jsonc) 是带注释的自定义配置参考；实际运行使用标准 JSON 格式的 `config.json` 覆盖默认值。只需填写要覆盖的字段，无需复制全部示例；复制字段时去掉注释，程序不直接加载这个 JSONC 参考文件。真实 Linux 配置在 `/opt/wxbridge/config.json`，本地修改不会自动同步。

## 配置如何生效

`config.defaults.json` → `config.json`（或 `--config` 指定的标准 JSON 文件）→ 环境变量覆盖 → CLI 参数及 `app/runtime.py` 强制规则。默认 JSON 按源码位置读取，与启动目录及自定义配置位置无关；`config.py` 只负责读取、合并、路径解析与校验，不再内嵌配置字典。默认文件缺失或损坏时明确报错，不静默换回隐藏的默认值。源码根目录的 `.env` 会补充尚未存在的环境变量，已有进程环境优先。`.env` 固定与 `README.md`、`config.example.jsonc` 同级，不受当前工作目录或 `--config` 路径影响。

两份 JSON 配置均在启动时加载，修改后需要重启机器人进程；如果 `config.json` 已填写同名项，会覆盖默认 JSON 中的修改。独立提示词文件的内容每次生成回复时读取，同一路径下的内容修改在下一次生成时生效。`--config` 可指定配置文件；相对的密钥、解密、状态、日志和提示词文件路径按配置文件所在目录解析。`db_dir` 建议填写绝对路径。API Key 仍放 `.env`，不放默认 JSON。

## 群聊和回复

`reply_style.wechat_emoticons_enabled` 默认false，可在实际配置中按需开启；允许少量微信内置文本表情和适当的单发表情。`reply_style.omit_trailing_period` 默认true，普通短句省略尾部中文句号，保留技术内容与其他标点。两项均须布尔值，改后重启监听；20项梗义、来源及处理范围见 [微信表情与标点](EMOTICONS.md)。

主动接话使用 `proactive` 配置段，默认关闭，可在实际配置中按需开启。保留近300秒十条文字的门槛，增加自然称呼/引用与低频多人聊天候选；每小时默认最多三次主动入场。确认发出后可在120秒内判断是否继续，最多额外两轮；没有新消息不请求模型，真实@优先，短句最多30字。全部参数、重启与失效规则见 [主动接话说明](PROACTIVE.md)。

人物记忆使用 `memory` 配置段：enabled（默认 true）、directory（源码根目录下 memory）、model（deepseek-flash）、thinking（enabled）、reasoning_effort（max）、min_interval_seconds（60）、prompt_max_chars（1200）、max_tokens（2400）、timeout_seconds（30）。thinking 可选 enabled/disabled，reasoning_effort 可选 low/high/max，关闭思考时不发送深度参数；记忆整理独立于主回答的 high。规则、期限和维护命令见 [MEMORY.md](MEMORY.md)。

`memory.avatar` 控制初见视觉参考：enabled（true）、max_per_batch（2）、max_bytes（524288）、retry_interval_seconds（3600）。只读取当前账号的头像缓存；文字印象及好感度请求看不到头像，视觉分类独立调用，失败不阻止文字整理。详见 [初见头像线索](MEMORY.md#初见头像线索)。

`memory.system_prompt_file`默认prompts/memory.md，独立编辑整理与学习提示词，下一批读取。`memory.learning`默认enabled=true、passive_enabled=true、window_seconds=300、min_new_messages=10、min_senders=2、min_interval_seconds=300。活跃闲聊也整理全部记忆；自动归纳需跨至少两天的证据才稳定采用，明确偏好和纠正立即处理。详见[语境学习](LEARNING.md)。

| 配置项 | 示例值 | 作用 |
| --- | --- | --- |
| `target.chat` | 群完整显示名 | 指定一个目标群，也可用群 ID；当前不是多群配置 |
| `target.bot_name` | `DeepSeek` | 填入系统提示词中的 `{bot_name}`，不会修改微信昵称或实际 @ 账号 |
| `poll_interval_seconds` | `2.0` | 接收调度等待上限；为了及时封批，实际最多等待 0.5 秒；API/UI 在子进程，不阻塞接收 |
| `target.cooldown_seconds` | `8` | 上一个分片在数据库确认发出后的最短间隔；冷却中的任务等待，设 `0` 取消等待 |
| `target.max_message_age_seconds` | `90` | 仅首次接收时过滤过旧触发，已接纳任务不因排队过期；未来/无效时间仍拒绝 |
| `target.context_max_chars` | `24000` | 格式化用户提示词的字符预算，包含上下文和触发等；超出则取消回复，非截断历史；正值启用预算 |
| `target.max_reply_chars` | `1800` | 每条发送分片的字符上限，超过会分多条；不是整次答案的总长度限制，使用正整数 |

提示词要求常规闲聊、日常问答约 10 字、尽量不超过 30 字；专业问题按需要展开。它是模型表达偏好，不强行截字。确需分两条时由模型用一个空行分成两段，发送层保持顺序、逐条确认与原冷却；代码块中的空行保留，超长内容仍按传输上限分片。`max_tokens` 是生成 token 上限，`max_reply_chars` 是微信发送分片上限，都不限制人格提示词文件长度。

## DeepSeek

| 配置项（均在 `deepseek` 下） | 示例值 | 作用 |
| --- | --- | --- |
| `model` | `deepseek-flash` | 请求的模型名称，须为所用服务实际支持的模型 |
| `thinking` | `enabled` | `enabled` / `disabled` 时发送相应参数；当前按用户要求开启试用 |
| `reasoning_effort` | `high` | 思考深度，可用 low/high/max；开启思考时传给模型，关闭时不发送 |
| `system_prompt_file` | `prompts/system.md` | 独立 UTF-8 人格提示词文件，支持 Markdown、多行和长文本；仅替换 `{bot_name}`，其余花括号原样保留 |
| `system_prompt` | 旧版兼容项 | 旧配置只有内联提示词时仍可使用；显式配置文件后以文件为准 |
| `temperature` | `1.0` | 传给模型的采样参数，支持范围由接口决定 |
| `max_tokens` | `1200` | 请求的最大输出 token 数，不等于中文字数 |
| `timeout_seconds` | `60` | 单次 HTTP 访问的超时参数，非整次处理的总时限 |
| `base_url` | `https://api.deepseek.com` | API 基础地址，程序会附加 `/chat/completions` |
| `api_key_env` | `DEEPSEEK_API_KEY` | 保存 API Key 的环境变量名；密钥放源码根目录的 `.env` 或进程环境，不在 JSON 中直接填写 |

每次模型请求最多尝试三次、重试间隔 1.5/3 秒目前写在 `ai/client.py`，没有配置项；开启搜索后还受整体时限约束。日志级别、日志 2 MiB 轮换及备份数量同样不是 JSON 配置项。

根目录 `.env` 的格式如下，等号后填实际密钥：

```dotenv
DEEPSEEK_API_KEY=
```

`.env.example` 只提供无密钥示例，`.env` 已被 Git 忽略；修改密钥后需重启进程。Linux 上对应 `/opt/wxbridge/.env`。用户此前授权的测试密钥已放在本地 `.env`；部署包与代码备份均排除凭据。

### 编写人格提示词

直接编辑 [prompts/system.md](../prompts/system.md)。可以按“身份与背景、性格与价值观、说话习惯、群友关系、互动示例、回复边界”等章节组织；普通换行、引号和 JSON 示例无需做 JSON 字符串转义。

```json
"system_prompt_file": "prompts/system.md"
```

路径相对 `config.json` 所在目录；文件支持 UTF-8 BOM。程序完整读取文件，不做长度截断，也不计入 `context_max_chars` 的用户提示词预算；总输入仍受模型上下文容量约束。文件不存在、编码错误或内容全为空白时取消该次回复，不静默换回其他人设。JSON 配置、模型和文件路径的变更仍需重启。

## 发送与界面

| 配置项（均在 `ui` 下） | 示例值 | 作用 |
| --- | --- | --- |
| `dry_run` | `true` | 不实际发消息，但可能仍调用模型；纯离线检查用测试脚本 |
| `backend` | `auto` | `auto` / `atspi` / `xdotool`，选择发送后端 |
| `input_method` | `auto` | 搜索与编辑器均传入此配置，可选 `atspi` / `xtype` / `clipboard` |
| `verify_chat_selected` | `true` | 正式入口强制开启，发送前确认目标群 |
| `chat_open_wait` | `1.0` | 选择或打开群后等待的秒数 |
| `post_send_wait` | `0.8` | UI 提交后读回草稿前的等待时间；发送成功仍以数据库为准 |
| `send_confirm_timeout_seconds` | `10` | UI 工作进程结束后等待发出记录的时间；重启恢复的意图也先核对此时间；超时暂停发送但继续接收 |
| `native_executable` | `/opt/wechat/wechat` | 窗口所属原生程序的路径，排除内置浏览器窗口 |
| `window_class`、`window_names` | `wechat` / 微信、WeChat | X11 窗口定位 |
| `app_names`、`chat_list_names` | 微信应用名 / 聊天列表名 | AT-SPI 应用和聊天列表定位 |
| `messages_list_names` | 消息列表名称 | 供 UI 诊断读消息列表使用，不控制数据库接收 |
| `search_shortcut` | `ctrl+f` | 搜索群的快捷键 |
| `paste_shortcut` | `ctrl+v` | 剪贴板输入时粘贴快捷键 |
| `select_all_shortcut` | `ctrl+a` | 输入框全选快捷键 |
| `send_key` | `Return` | 部分备用发送路径使用的按键；AT-SPI 主路径点击按钮 |

`run --send` 强制当前进程 `dry_run=false`，`run --dry-run` 强制为 `true`；`scripts/run-live.py` 固定传入 `--send`。因此该服务入口下，仅改配置中的 `dry_run` 不能关闭发送，应停止服务或使用明确的 dry-run 启动命令。

发送按钮只使用明确的点击/按下/激活动作，`SetFocus` 仅代表聚焦。没有点击动作时，改用实际命中原生主窗口的坐标点击；窗口遮挡则停止。动作与草稿长度元数据即使晚于数据库确认返回，也会保存，便于排查。

鼠标移动后核对实际坐标，不用会在同坐标等待的 `mousemove --sync`。明确记录为“尚未提交”的临时 UI 失败最多延迟 2 秒重试两次（目前是固定策略）；草稿冲突、已点击或提交状态不明不适用该自动重试。

## 路径

| 配置项 | 作用 |
| --- | --- |
| `db_dir` | 当前微信账号的 `db_storage`，留空自动探测 |
| `keys_file` | SQLCipher 数据库密钥文件，与 DeepSeek API Key 不同 |
| `decrypted_dir` | 解密副本目录，必须在当前用户所有、0700 的 Linux `tmpfs,noswap` 中 |
| `state_file` | 迁移时读取旧游标；运行中状态写旁边 `live-status.json`，队列才是游标与冷却的权威来源 |
| `log_file` | 应用日志位置 |

示例运行数据均放 `/run/wxbridge-trial`；这些目录和权限需先准备好，改路径不等于自动创建部署环境。

## 配置里出现但当前入口不自由切换的项

| 项目 | 当前行为 |
| --- | --- |
| `source` | 整理版只接受 `db` |
| `target.trigger.require_mention_metadata` | 固定 `true`，要求真实 @ 的账号 ID |
| `target.trigger.reply_all` | 固定 `false` |
| `target.trigger.prefixes` | 固定空列表 |
| `target.trigger.mentions` | 兼容文本触发的名称列表；当前真实 @ 模式不靠这个判断 |
| `reply_to_backlog` | 固定 `false`；有队列时恢复，有旧状态时迁移游标，全新启动才建立新基线 |
| `wechat_process` | 遗留默认字段，当前链路没有使用 |

这些字段不能仅改 JSON 就改变实际策略。

`target.context_window_seconds` 已恢复为可调整的正整数，默认 `300`（5 分钟），不再由 runtime 强制覆盖。前台回答和后台记忆整理使用同一窗口，修改后重启监听生效。长期人物记忆仍独立保留；明确引用的消息可以早于该窗口。

`target.trigger.pat_enabled` 默认 `true`。其他群成员拍当前机器人时独立入队，读取最近上下文并自然回应；自己拍、拍其他人、错误群或不明确的事件不触发。拍一拍与文字 @ 不合并，仍遵守队列、冷却及数据库发送确认。设为 `false` 并重启可关闭拍一拍回复。

## 合并、队列和恢复

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `queue.debounce_seconds` | `0` | 按用户最新选择关闭合并，每次真实 @ 独立排队，无静默等待；将来设 `3` 可重新启用同人 3 秒合并，普通聊天不重置 |
| `queue.generation_concurrency` | `2` | 同时运行的有效生成进程上限；更新批次会终止旧本地进程，服务端旧请求可能继续计费 |
| `queue.file` | `/run/wxbridge-trial/queue.sqlite3` | 当前用户所有的 0600 SQLite；父目录必须私有 0700 且位于 `tmpfs,noswap`，不允许磁盘回退 |

默认每次 @ 独立入队、按接收顺序发送。只有显式启用合并窗口时才按窗口结束时间排队。最新版本生成完成、轮到队首且冷却结束才可提交。上下文以最后一条触发的实际发送时间取五分钟；启用合并时从历史去重全部触发问题。

`python3 -m wxbridge queue-status` 只读队列元数据，不读微信数据库或显示正文。进程重启保留任务及分片进度，主机重启清空内存队列。恢复时核对微信账号与群；待确认意图先核对发出记录，不盲目重试。确认超时仍接收和生成新任务，后续发送等待；晚到的匹配发出记录可自动解除该阻塞。

`generation:*` 表示生成失败，任务保留且队首可见；应先解决模型/提示词问题。未知发送状态需要检查草稿和发出记录后人工处理，不能用删除队列来假装发送成功。`--once` 仅运行一个调度回合，子进程会停止，任务保留。

dry-run 使用独立的 `.dry-run` 队列及 `dry-run/live-status.json`，不消费生产任务。生产和模拟监听仍对共享解密目录互斥。

## 引用回复

引用一条消息后真实 @ 助手，可以直接问“这怎么看”。系统会把本次问题与被引用的正文、作者和原消息时间分别提供；明确引用可早于五分钟历史窗口，文字仍计入字符预算。支持文字、链接快照及被明确引用的本地图片；语音等暂不识别。详见 [引用消息说明](REFERENCES.md)。

`queue-status` / `live-status.json` 的 `recent_intake` 显示最近消息的接纳或跳过原因、真实 @ 标记及引用字符数，不包含正文。

## 图片理解

`vision.enabled=true` 默认启用引用图片；`detail=original`，`max_images=4`，`max_image_bytes=8388608`，`cache_wait_seconds=3`。图片只在工作进程内解密，作为内联数据发送给当前模型；日志、队列和磁盘不保存图片明文。`last_vision.images` 可区分是否实际附送了图片。完整配置范围和缓存限制见 [图片说明](VISION.md)。

## 按需联网搜索

`search.enabled=true` 默认开启，继续使用当前模型和同一 DeepSeek Key。默认 `max_tool_rounds=2`、`max_requests=3`、`max_results=5`、单工具请求超时 15 秒、整体生成预算 120 秒。普通聊天不搜索；搜索时单独发送一次 `feedback_phrases` 中的短句，可用 `feedback_enabled=false` 关闭提示。提示仍遵守当前冷却、队列及发送确认，不会消耗最终回答分片。完整范围、错误行为和费用说明见 [搜索说明](SEARCH.md)。

## 环境变量覆盖

`WX_GROUP` 覆盖目标群，`WX_DB_DIR` 覆盖数据库目录，`DEEPSEEK_MODEL` 覆盖模型，`DEEPSEEK_BASE_URL` 覆盖 API 地址。API Key 的变量名由 `api_key_env` 指定。
