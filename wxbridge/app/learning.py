"""活跃闲聊登记记忆任务；只由主进程读取数据库，不额外生成发言。"""
from wxbridge.ai.context import message_time
from wxbridge.wechat.pats import is_pat


def eligible_messages(rows, account, now, settings, floor, processed=()):
    seen, result = set(), []
    for m in sorted(rows, key=lambda m: m['local_id']):
        sent = message_time(m)
        if (m['local_id'] <= floor or m['local_id'] in processed or not m.get('sender')
                or m['sender'] == account or m.get('type') != 1 or is_pat(m)
                or sent is None or not now-settings['window_seconds'] <= sent <= now):
            continue
        text = (m.get('text') or '').strip()
        key = (m['sender'], text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(m)
    return result


def enqueue_if_active(bot, now):
    settings = bot.cfg['memory']['learning']
    if (not settings['enabled'] or not settings['passive_enabled'] or not bot.memory
            or bot.journal.tasks() or bot.generators or bot.ui_job):
        return
    state = bot.journal.get('passive_learning', {})
    floor = max(bot.memory.data['watermark'], bot.memory.data.get('learning', {}).get('started_after_id', bot.last_local_id),
                state.get('started_after_id', 0))
    if bot.last_local_id <= floor:
        return
    if (bot.last_local_id <= state.get('checked_id', 0) or now-state.get('checked_at', 0) < 10
            or now-state.get('enqueued_at', 0) < settings['min_interval_seconds']
            or any(j.get('origin') == 'activity' for j in bot.journal.memory_jobs())):
        return
    start, end = int(now-settings['window_seconds']), int(now)
    rows = [m for m in bot.store.messages_in_window(bot.chat_id, start, end) if m['local_id'] <= bot.last_local_id]
    messages = eligible_messages(rows, bot.store.self_wxid, now, settings, floor, bot.memory.data.get('processed_ids', []))
    with bot.journal.db:
        state.update(checked_id=bot.last_local_id, checked_at=now, new_messages=len(messages), error='')
        if len(messages) >= settings['min_new_messages'] and len({m['sender'] for m in messages}) >= settings['min_senders']:
            last = max(messages, key=lambda m: m['local_id'])
            bot.journal.enqueue_memory('activity', 'activity:'+str(last['local_id']),
                [dict(local_id=last['local_id'], time=last['time'])], [start, end])
            state.update(enqueued_at=now, through=last['local_id'])
        bot.journal.set('passive_learning', state)
