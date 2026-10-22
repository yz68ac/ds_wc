"""确认发出后的会话策略；纯函数，返回需与发送确认共同提交的元数据。"""


def confirmation_updates(task, local_id, now, previous, entries, *, settings):
    changes = {}
    if not settings or not settings['enabled'] or task.get('generation_failed'):
        return changes
    data = task.get('proactive', {})
    optional = task.get('trigger_kind') == 'proactive'
    mode = data.get('mode', 'entry')
    if optional and mode == 'entry':
        changes['proactive_entries'] = [t for t in entries if 0 <= now-t < 3600]+[now]
    if not settings['conversation']['enabled']:
        changes['proactive_session'] = None
        return changes
    participant = data.get('recipient') if optional else task['sender']
    if not participant:
        return changes
    followups = 0
    if optional and mode == 'followup':
        if not previous or previous['anchor_id'] != data['anchor_id']:
            return changes
        followups = previous['followups']+1
    if followups >= settings['conversation']['max_followups']:
        changes['proactive_session'] = None
    else:
        changes['proactive_session'] = dict(anchor_id=local_id, participant=participant,
            expires_at=now+settings['conversation']['window_seconds'], followups=followups,
            origin='entry' if optional and mode == 'entry' else 'reply' if not optional else previous['origin'])
    return changes
