> 历史参考，归档于 2026-10-29。正文保留当时的研究、实验或运维结论；接口、路径、测试数量和状态可能已过时。当前接口以 [wechat_linux 状态](../WECHAT_LINUX.md) 和 [provider 用法](../PROVIDER.md) 为准。

# 2026-10-28 架构改进

本次保持现有启动命令、单读库所有者、JSON 队列和记忆文件格式；以修复已确认的生命周期问题和收口跨模块接口为主。没有引入新服务或消息中间件，没有修改微信原生 helper、JS、C 或二进制版本指纹。

## 已实施

| 审阅项 | 改动 | 验证入口 |
| --- | --- | --- |
| A01 进程漏登记 | WorkerLease 接管新进程，登记失败关闭；无法确认退出则抛安全致命错误，停止准入；ExitStack 尝试全部资源清理 | `test_worker_lifecycle.py` |
| A02 完成结果被抢占 | 先收集已完成记忆，再抢占仍在运行的后台任务；成功结果不重新调用 API | `test_worker_lifecycle.py` |
| A03 调度耦合 | 单群与多群使用 SessionPhases 公开阶段；账号调度使用占用、准入、发送门等公开能力；点名判断不被同群新真实 @ 静默取消 | `test_worker_lifecycle.py`、`test_instances.py`、`test_keyword_participation.py` |
| A04 记忆职责 | 只读 MemoryView、确定性 memory_rules 与纯文本 memory_render 分开；AI 快照不读取 MemoryStore.data；同一轮候选共用快照；存储不导入模型层 | `test_memory_contracts.py`、原记忆/来源/关系测试 |
| A05 提示词组装 | 输出、搜索、主动参与使用命名策略片段；媒体通过带来源指纹的槽位更新，保留成员映射、历史及外部附加内容；控制最终文本预算 | `test_prompt_contracts.py`、原图片/搜索/上下文测试 |
| A06 配置继承 | 加载时编译各角色最终设置；保留原始字典和运行时修改兼容；新增 explain-config 只读来源诊断；config 与 instances 不再互相导入 | `test_effective_models.py`、`test_explain_config_cli.py` |
| A07 存储穿透 | QueueInspection 集中只读摘要；发布、恢复及记忆维护使用同一接口；Journal 提供轻量计数/候选/意图查询 | `test_queue_inspection.py`、原发布/恢复测试 |
| A08 故障边界 | 已明确无副作用的状态快照写失败可降级并报告；读库、队列、身份及未知错误仍停止运行 | `test_worker_lifecycle.py` |
| A09 队列查询 | 有界 generation_candidates 和索引；has_intent 等不反序列化全部任务；增加合成积压基准 | `benchmark-queue.py`、`test_journal.py` |
| A10 SDK 边界 | NativeOptions 与显式媒体 runtime_dir/max_bytes；保留旧接口；能力接口只说明 SDK；CI 新增最低版本与隔离 wheel 检查 | `test_sdk_options.py`、`smoke-wheel.py`、CI sdk-wheel |

## 兼容与故障处理

旧配置、旧模块导入和 MemoryStore 的 prompt/background/reply_relationship 方法继续兼容。新代码优先使用明确快照；MemoryView 不跨进程，真正传给记忆 worker 的仍是可序列化的普通 JSON 数据，原修订号、水位、来源及稳定成员 ID 校验保持。

旧普通列表提示词保留图片重建兼容路径；正式 build_prompt 返回的 PromptMessages 已登记引用位置。账号、触发身份、引用指纹或文本内部布局不符时拒绝替换，不猜测匹配。元数据增长优先减少完整记忆行，`recent` 策略才移除完整旧历史，提问和引用本身不截断。图片像素不写入队列或诊断日志。

Journal 和只读摘要打开时统一验证任务状态，put 写入前也验证。缺失或未知 phase、非 object/null intent、已完成却残留 intent、坏 JSON 均拒绝，不因轻量 SQL 返回空就视作可以发送。未知发送结果继续关闭全账号发送门，不自动重发、不回退桌面。

WorkerCleanupError 属于停止运行的错误，不能按普通模型错误兜底或重试。状态文件写失败的降级只包围诊断文件 I/O，不吞掉队列查询异常。启用状态下保护点名判断，不保护已经过时的可选插话正文；所有获准回复仍需原时效、来源和发送确认。

## 有意保留的后续工作

GroupSession 仍拥有生成和记忆的主要业务，ProactiveController 仍持有 session。本轮先建立公开阶段与只读数据边界，没有将所有业务类一次拆完。后续按真实扩展需求进一步提取控制器。

群级隔离只覆盖可以明确安全继续的诊断写失败。队列损坏或无法证明没有发送意图时仍整体停止；更广泛的群降级需要明确的恢复协议，不能用宽泛异常捕获代替。

没有修改 WeChatStore 联系人、消息表或 WAL 缓存。队列基准仅测合成内存 SQLite 查询，不能据此声称降低了同等比例的线上 CPU 或微信解密耗时。完整存储格式迁移和配置热重载均不属于本轮。

## 验证方式

本地统一回归：`python -X utf8 -B scripts/test-offline.py`。库发行验证：构建 wheel 后运行 `python scripts/smoke-wheel.py <wheel>`；它会在临时新环境无依赖安装并隔离导入，读取 JS/C/profile 资源，不连接客户端。

合成查询基准：`python scripts/diagnostics/benchmark-queue.py`，覆盖 1/4/16 群以及每群 100/1000 条积压。数据不包含真实群、正文或密钥。CI 另运行 Python 3.11/3.14 的隔离 wheel 检查；本机版本测试不能替代最低版本的真实执行。

代码变更与线上部署分别验收；本文件描述代码行为，不代表运行中的 Linux 已自动更新。
