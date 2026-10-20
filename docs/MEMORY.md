# 按群成员 ID 的记忆

开发前基线：`eb5ac6a`（153 项既有离线测试通过；凭据、实际配置和归档未提交）。

## 一次问答的时序

主进程用 `Name2Id.user_name`（通常为 wxid）识别人。记忆按机器人账号、群 ID、成员 wxid 隔离；昵称可变、同名不会混淆，每人保存最近五个曾用名。一次请求中使用 U1、U2 等一致编号区分成员及已知引用作者。

当前回答读取已有记忆。最终分片经数据库确认后，同事务登记后台任务再清理触发正文；搜索提示不登记任务。主进程按原触发时间重读十分钟窗口，后台只整理本人直接发言及来源，不从引用转述、机器人想象中提取现实事实。没有真实 @ 不新建整理任务；已登记任务可重试，不预先扫描全部历史。

后台默认 deepseek-flash、关闭思考、2400 tokens、30 秒超时。前台保持 high；后台最多一个，成功更新间隔至少 60 秒。前后台 API 总并发最多两个，前台需要位置时终止后台本地进程并重新排队；服务端请求可能仍会结束。后台失败只延后重试，不阻止接收或回复。积压按任务顺序处理。

## 数据与期限

源码根目录 `memory/<账号和群的哈希>/memory.json` 是权威数据；`additional_prompt.md` 是可重建的阅读投影，不宜手工编辑。JSON 内含完整作用域身份并在加载时校验。目录 0700、文件 0600（Linux）；Windows 的 chmod 不提供 POSIX 权限保证，正式监听仍仅支持 Linux。整个 memory 目录已加入 Git 忽略。

每人有 nickname、aliases、entries、affinity、last_interaction、daily 和 affinity_evidence。每条摘要有 id、kind、mode、summary、sources、occurred_at、confirmed_at、confidence；来源 local_id 在账号和群作用域内解释。occurred_at 采用首次直接来源消息的时间，表示当时获知此事；没有确切证据时不伪造事件实际发生日期。再次确认保留原始时间、单独更新 confirmed_at。人工纠正另记 corrected_at。精确时间仅用于存储与校验，前台按程序转换为今天、昨天、这几天、上周、前阵子；投影标注生成时间。

scene 保留 6 小时，event 30 天，preference/impression 90 天。事件发生时间和新证据再次确认时间分开；旧记忆被读取不会续期。每人事件最多 10 条、偏好 5 条、印象 3 条，短期情景另设 10 条上限。先清过期，再按证据强度与新旧淘汰；运行时每分钟清理已过期摘要，后台工作进行中推迟清理以免改动其修订号。无后台任务也不调用模型清理。

impression 始终作为暂时推测，real/roleplay 分开；不保存整段聊天、图片或内部推理。运行日志与状态只有计数、阶段、修订号及安全错误码。记忆会通过现有 API 发送给模型，目录应按私人数据保管。

## 好感与注入

初始 50，范围 0–100，每批每人最多 ±3，每个自然日累计绝对变化不超过 10；只依据该成员实际发言，每个新来源仅处理一次。连续 14 天无互动后，每再满 7 天向 50 回归 1。计算使用主机本地时区；前台显示只表达关系态度，不报告数值。

0–29 疏远，30–69 普通，70–100 亲近。疏远时主模型可以短答、少延伸、自然表达不想接话；仍须明确回应，不用固定冷淡话术、不故意答错。核心人格独立，参考近期回答避免复读。

每轮人物记忆摘要最多 1200 字符，先提问者，再选四位以内当前上下文相关成员；超预算按完整记忆行减少，不截断原十分钟历史。附加内容只是背景数据。图片重建、搜索工具循环均保留同一记忆和成员编号。

## 配置与维护

`config.defaults.json` / `config.json` 的 memory 配置：enabled、directory、model、min_interval_seconds（默认60，最小60）、prompt_max_chars（默认1200，上限1200）、max_tokens（默认2400）、timeout_seconds（默认30）。directory 相对源码根目录；修改配置后重启。可见状态在 live-status.json 的 memory 和 queue-status 的 memory_pending/memory_error。

以下在 Linux 源码目录、使用服务账号及原运行环境执行：

```bash
python3 -m wxbridge memory status
python3 -m wxbridge memory show --member wxid_xxx
```

show 会显示私有记忆正文，勿粘贴到公开日志。修改命令必须先停止监听，会取得相同队列及解密目录锁，正在监听时明确拒绝：

```bash
python3 -m wxbridge memory correct --member wxid_xxx --entry 记录id --text-file correction.txt
python3 -m wxbridge memory forget --member wxid_xxx --entry 记录id
python3 -m wxbridge memory forget --member wxid_xxx
python3 -m wxbridge memory clear
```

维护后再启动监听。修改命令保留并推进处理水位至当前接收游标，旧后台任务及同一旧消息不能重新学回被删内容。该水位对本群生效，因此维护前已接收的旧任务不会再生成其他新记忆；之后的新发言可重新学习。correct 不伪造新的消息确认时间。

memory.json 持久保存；后台任务指针和接收游标在私有 tmpfs SQLite 中，进程重启可恢复，主机重启清空。JSON 先写成功但队列尚未完成时崩溃，水位会让重试跳过已处理来源，避免重复记事和计分。微信晚同步的较早消息可能拥有比触发更大的 local_id，额外用 processed_ids 保存水位之外已处理的来源，避免重叠窗口重复计分；水位超过后自动移除这些例外 ID。不要手工回退 JSON 的 revision/watermark。
