"""Text presentation for the CLI status panel; no business controllers or database access."""
from __future__ import annotations

from datetime import datetime

from wxbridge.ai.roles import ROLES, model_settings
from wxbridge.app.status.snapshot import UNITS, mapping, numeric, number, code, error

PAGES = ('概览', '收发队列', '模型用量', '记忆与参与', '运行事件')
PHASES = {'listening':'等待新消息', 'generating':'生成回复', 'queued':'等待发送', 'ready':'准备发送',
          'cooldown':'等待发送冷却', 'confirming':'核对发出记录', 'blocked':'发送阻塞',
          'generation_retry_wait':'等待生成重试', 'stopped':'已停止', 'debouncing':'等待合并窗口',
          'search_notice_pending':'搜索提示待发送', 'open':'接纳提问', 'done':'已完成',
          'evaluating':'判断是否介入', 'generating_reply':'生成接话正文', 'silent':'本轮保持沉默',
          'waiting_new_messages':'等待新消息', 'waiting_conversation_reply':'等待可接续的新话',
          'entry_cooldown':'等待自主入场冷却', 'evaluation_cooldown':'等待判断间隔',
          'below_threshold':'未达到入场条件', 'foreground_or_disabled':'前台优先或功能关闭',
          'reply_queued':'等待完整回复', 'waiting':'等待新消息'}
FILE_ERRORS = {'missing':'尚未生成', 'denied':'权限不足，请使用 root 或 wxbridge 账号',
               'invalid':'格式或文件类型无效', 'large':'文件超过读取上限', 'unavailable':'读取失败'}


def items(value, limit=100):
    return value[:limit] if isinstance(value, list) else []


def milliseconds(value):
    return f'{value:.2f}' if numeric(value) else '-'


def yes(value):
    return '是' if value is True else '否' if value is False else '-'


def when(value):
    try:
        return datetime.fromtimestamp(value).strftime('%m-%d %H:%M:%S') if numeric(value) and value else '-'
    except (OSError, OverflowError, ValueError):
        return '-'


def phase(value):
    return PHASES.get(value, code(value)) if isinstance(value, str) else '-'


def health(snapshot, now):
    s = mapping(snapshot.get('live'))
    if snapshot.get('file_error'):
        return '状态不可读：'+FILE_ERRORS.get(snapshot['file_error'], '读取失败')
    updated = s.get('updated_at')
    if not numeric(updated) or now-updated < -5:
        return '状态时间无效或系统时钟不一致'
    if s.get('phase') == 'stopped':
        return '监听已停止'
    if now-updated > 10:
        return f'心跳已过期 {now-updated:.0f} 秒'
    bot = mapping(mapping(snapshot.get('services')).get(UNITS[0]))
    if bot and bot.get('ActiveState') != 'active':
        return '监听服务未运行，显示最后快照'
    if bot.get('MainPID', '0').isdigit() and int(bot.get('MainPID', '0')) and int(bot['MainPID']) != s.get('pid'):
        return '进程已变化，等待当前进程快照'
    return '心跳正常'


def remaining(session, now):
    value = session.get('expires_at')
    return f'{max(0, value-now):.0f} 秒' if session.get('active') and numeric(value) else '-'


def pages(snapshot, cfg, *, now=None):
    now = snapshot['collected_at'] if now is None else now
    s, services = mapping(snapshot.get('live')), mapping(snapshot.get('services'))
    mem, participation = mapping(s.get('memory')), mapping(s.get('participation'))
    decision_required = participation.get('decision_required')
    reply_flow = ('私聊直接回复' if decision_required is False else
                  '群聊参与判断' if decision_required is True else '按实例配置分流')
    build = mapping(s.get('build'))
    summary = [health(snapshot, now), f'阶段：{phase(s.get("phase"))}    PID：{number(s.get("pid"))}',
        f'版本：{code(build.get("commit"))[:12]}    启动：{when(s.get("started_at"))}',
        f'快照：{when(s.get("updated_at"))}    模拟发送：{yes(s.get("dry_run"))}', '']
    for label, unit in zip(('机器人', '微信会话', '开机恢复'), UNITS):
        value = mapping(services.get(unit))
        summary.append(f'{label}：{code(value.get("ActiveState"))}/{code(value.get("SubState"))}'
                       + (f'  重启 {code(value.get("NRestarts"))} 次' if 'NRestarts' in value else ''))
    if snapshot.get('service_error'):
        summary.append('主控状态不可用；仍可查看状态文件')
    summary += ['', f'回复队列：{number(s.get("queue_length"))}    生成进程：{number(s.get("generation_workers"))}',
        f'最近发出确认：{when(s.get("last_success"))}    接收游标：{number(s.get("cursor"))}',
        f'发送阻塞：{error(s.get("blocked_reason"))}',
        f'记忆待整理：{number(s.get("memory_pending"))}    正在整理：{yes(mem.get("running"))}',
        f'记忆错误：{error(s.get("memory_error") or mem.get("error"))}',
        f'自动回复：{yes(participation.get("enabled"))}    '
        + reply_flow,
        f'待判断：{number(participation.get("pending", 0))}    判断中：{number(participation.get("running", 0))}',
        f'开机恢复阶段：{phase(mapping(snapshot.get("boot")).get("phase"))}']
    queue = [f"发送后端：{code(s.get('configured_backend'))}    拥塞：{code(mapping(s.get('backpressure')).get('reason'))}",
             f"最近明确失败：{code(mapping(s.get('last_delivery_failure')).get('error'))}",
             '任务   触发       版本  阶段 / 生成      分片进度   重试次数']
    for task in items(s.get('tasks')):
        t = mapping(task)
        queue += [f'{number(t.get("id")):<6} {code(t.get("trigger_kind")):<10} {number(t.get("version")):<4}'
                  f' {phase(t.get("phase"))}/{code(t.get("generation"))}'
                  f'   {number(t.get("next_chunk"))}/{number(t.get("chunks"))}   {number(t.get("generation_attempts"))}',
                  f'  阻塞：{error(t.get("blocked_reason"))}  生成错误：{error(t.get("last_generation_error"))}']
    if not items(s.get('tasks')):
        queue.append('当前没有待回复任务')
    queue += ['', '最近接收判定（不含正文）', '消息ID       原因']
    for event in reversed(items(s.get('recent_intake'), 20)):
        e = mapping(event)
        queue.append(f'{number(e.get("local_id")):<12} {code(e.get("reason"))}')
    models = ['角色          调用  失败      输入tokens   输出tokens    API累计秒',
              '统计属于当前监听进程；被取消的请求用量可能缺失']
    statistics = mapping(mapping(s.get('metrics')).get('models'))
    for role in ROLES:
        value = mapping(statistics.get(role))
        elapsed = value.get('elapsed_ms', 0)
        models.append(f'{role:<13} {number(value.get("calls", 0)):>4} {number(value.get("errors", 0)):>5}'
                      f' {number(value.get("prompt_tokens", 0)):>12} {number(value.get("completion_tokens", 0)):>12}'
                      f' {elapsed/1000 if numeric(elapsed) else 0:>12.1f}')
    models += ['', '最近聊天上下文（字符数，不含系统提示词和工具）']
    for role in ('participation', 'reply'):
        value = mapping(mapping(s.get('context')).get(role))
        models.append(f'{role}: 字符 {number(value.get("chars"))}  历史 {number(value.get("retained"))}'
                      f'/{number(value.get("selected"))}  裁剪 {number(value.get("omitted"))}'
                      f'  扫描 {number(value.get("scanned"))}  扫描上限 {yes(value.get("scan_limit_hit"))}'
                      f'  错误 {error(value.get("error"))}')
    models += ['', '面板启动时的配置参考；配置变更需重启监听生效']
    for role in ROLES:
        setting = model_settings(cfg, role)
        thinking = code(setting.options.reasoning_effort) if setting.options.thinking == 'enabled' else '关闭思考'
        models.append(f'{role}: {code(setting.model)}  {thinking}'
                      f'  上限 {number(setting.options.max_tokens)} tokens / {number(setting.options.timeout_seconds)} 秒')
    models += ['', '主进程阶段耗时（毫秒）']
    for name, entry in mapping(mapping(s.get('metrics')).get('stages')).items():
        v = mapping(entry)
        models.append(f'{code(name):<16} 次数 {number(v.get("count"))}  最近 {milliseconds(v.get("last_ms"))}  最大 {milliseconds(v.get("max_ms"))}')
    detail = [f'记忆启用：{yes(mem.get("enabled"))}    整理中：{yes(mem.get("running"))}',
        f"已隔离任务：{len(items(s.get('memory_quarantined')))}",
        f'待整理：{number(s.get("memory_pending"))}    错误：{error(s.get("memory_error") or mem.get("error"))}',
        f'成员档案：{number(mem.get("members"))}    有可用摘要：{number(mem.get("members_with_memories"))}    修订：{number(mem.get("revision"))}',
        f'摘要：{number(mem.get("entries"))}    临时编号歧义隔离：{number(mem.get("ambiguous_entries"))}',
        f'记忆水位：{number(mem.get("watermark"))}    头像线索：{number(mem.get("initial_impressions"))}',
        f'表达候选：{number(mem.get("learning_candidates"))}    已稳定：{number(mem.get("learning_stable"))}']
    recovery = mapping(s.get('memory_recovery'))
    if recovery:
        detail += [f'最近批次恢复：任务 {number(recovery.get("task_id"))} / {code(recovery.get("stage"))}',
                   f'  原因：{error(recovery.get("reason"))}']
    counts = mapping(participation.get('counts'))
    detail += ['', f'自动回复启用：{yes(participation.get("enabled"))}    {phase(participation.get("outcome"))}',
        '回复流程：'+reply_flow,
        f'待判断：{number(participation.get("pending", 0))}    判断中：{number(participation.get("running", 0))}',
        f'获准：{number(counts.get("approved", 0))}    沉默：{number(counts.get("silent", 0))}',
        f'过期：{number(counts.get("expired", 0))}    失败：{number(counts.get("error", 0))}    取消：{number(counts.get("cancelled", 0))}',
        f'最近判断耗时：{milliseconds(participation.get("last_duration_ms"))}',
        f'参与错误：{error(participation.get("error"))}']
    logs = ['近期安全运行事件（最新在前；仅显示已知事件及字段）', '不展示聊天正文、原始异常或未识别日志', '']
    logs += snapshot.get('events') or ['暂无可显示事件']
    if snapshot.get('log_error'):
        logs.append('日志：'+FILE_ERRORS.get(snapshot['log_error'], '读取失败'))
    return (summary, queue, models, detail, logs)
