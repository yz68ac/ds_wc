"""成员活动驱动的参与窗口；纯策略由接收/确认事务写入同一队列。"""

from wxbridge.ai.context import message_time
from wxbridge.wechat.pats import is_pat

SESSION_POLICY = 2


def observation_updates(message, accepted, now, previous, *, settings, account):
    if not settings or not settings['enabled'] or not settings['conversation']['enabled']:
        return {}
    sent = message_time(message)
    if (sent is None or sent > now or not message.get('sender') or message['sender'] == account
            or is_pat(message) or message.get('type') not in (1, 3, 34, 42, 43, 47, 48, 49, 62)
            or (message['type'] == 1 and not (message.get('text') or '').strip())):
        return {}
    identifier = message['local_id']
    seconds = settings['conversation']['window_seconds']
    if accepted and (account in (message.get('mentioned_users') or []) or message.get('addressed')):
        return {'proactive_session': dict(policy=SESSION_POLICY, session_id='mention:'+str(identifier),
            anchor_id=identifier, start_id=identifier, participant=message['sender'],
            expires_at=sent+seconds, last_activity_at=sent, last_activity_id=identifier,
            followups=0, origin='addressed' if message.get('addressed') else 'mention')}
    if not previous or identifier <= previous['last_activity_id']:
        return {}
    if sent >= previous['expires_at']:
        # 新消息跨过了两分钟空档：旧会话结束，本条只能参加新的入场筛选。
        return {'proactive_session': None, 'proactive_entry_floor': identifier-1}
    return {'proactive_session': {**previous,
        'last_activity_at': max(previous['last_activity_at'], sent), 'last_activity_id': identifier,
        'expires_at': max(previous['expires_at'], sent+seconds)}}


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
    newer_invitation = (previous and previous.get('origin') == 'mention'
                        and previous['start_id'] > data.get('snapshot_id', 0))
    if optional and mode == 'entry' and not newer_invitation:
        participant = data.get('recipient')
        if participant:
            changes['proactive_session'] = dict(policy=SESSION_POLICY, session_id='entry:'+str(task['id']),
                anchor_id=local_id, start_id=local_id, participant=participant,
                expires_at=now+settings['conversation']['window_seconds'],
                last_activity_at=now, last_activity_id=local_id, followups=0, origin='entry')
    elif previous and now < previous['expires_at']:
        # 普通 @ 的回复与接话只更新已确认锚点，不替成员延长计时，也不重开过期窗口。
        session = dict(previous, anchor_id=local_id)
        if optional and mode == 'followup' and data.get('session_id') == previous['session_id']:
            session.update(followups=previous['followups']+1, participant=data['recipient'])
        limit = settings['conversation']['max_followups']
        changes['proactive_session'] = None if limit and session['followups'] >= limit else session
    return changes
