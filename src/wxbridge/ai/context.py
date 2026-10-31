"""Bounded, source-anchored snapshots; no model or worker-side database I/O."""
from copy import deepcopy
from wechat_linux.messages.pats import is_pat

PAGE_SIZE = 200
SCAN_LIMIT = 1000


class ContextError(ValueError):
    def __init__(self, message, *, code='ContextError'):
        super().__init__(message)
        self.code = code


class ContextRows(list):
    """Private selection provenance; never serialized into model messages."""
    def __init__(self, rows=(), *, recent_ids=(), bot_groups=(), scanned=0, scan_limit_hit=False):
        super().__init__(deepcopy(list(rows)))
        self.recent_ids = tuple(recent_ids)
        self.bot_groups = tuple(tuple(group) for group in bot_groups)
        self.scanned = scanned
        self.scan_limit_hit = scan_limit_hit

    def include(self, rows):
        return ContextRows([*self, *rows], recent_ids=self.recent_ids, bot_groups=self.bot_groups,
                           scanned=self.scanned, scan_limit_hit=self.scan_limit_hit)


def message_time(message):
    value = message.get('time')
    if message.get('time_source') in ('unknown', 'observed'):
        return None
    return value if type(value) is int and value > 0 else None


def message_key(message):
    sent, identifier = message_time(message), message.get('local_id')
    if sent is None or type(identifier) is not int or identifier < 1:
        raise ContextError('消息缺少可靠的发送时间或ID，取消请求')
    return sent, identifier


def source_anchor(triggers):
    if not triggers:
        raise ContextError('缺少本轮消息')
    return max(triggers, key=message_key)


def memory_bounds(cfg, trigger):
    end = message_key(trigger)[0]
    return end-cfg['memory']['context_window_seconds'], end


def valid_message(message):
    return (message_time(message) is not None
            and type(message.get('local_id')) is int and message['local_id'] > 0
            and isinstance(message.get('sender'), str) and bool(message['sender'])
            and (is_pat(message) or message.get('type', 1) == 1
                 and isinstance(message.get('text'), str) and bool(message['text'].strip())))


def select_memory_context(cfg, context, trigger):
    start, end = memory_bounds(cfg, trigger)
    key = message_key(trigger)
    return sorted((m for m in context if valid_message(m) and start <= m['time'] <= end
                   and m['local_id'] != trigger['local_id'] and message_key(m) < key), key=message_key)


def select_context(cfg, context, trigger, *, triggers=None, account=''):
    sources = triggers or [trigger]
    anchor = source_anchor(sources)
    upper = message_key(anchor)
    watermark = max(m['local_id'] for m in sources)
    required = {m['local_id'] for m in sources}
    start = upper[0]-cfg['target']['context_max_age_seconds']
    rows = {}
    for message in context:
        if (valid_message(message) and message['local_id'] not in required
                and message['local_id'] <= watermark and start <= message['time']
                and message_key(message) < upper):
            identifier = message['local_id']
            if identifier in rows and rows[identifier] != message:
                raise ContextError('invalid_chat_context_source')
            rows[identifier] = message
    ordered = sorted(rows.values(), key=message_key)
    if isinstance(context, ContextRows):
        return ContextRows(ordered, recent_ids=context.recent_ids, bot_groups=context.bot_groups,
                           scanned=context.scanned, scan_limit_hit=context.scan_limit_hit)
    recent = ordered[-cfg['target']['context_recent_messages']:]
    positions = [i for i, m in enumerate(ordered)
                 if account and m['sender'] == account and not is_pat(m)]
    count = cfg['target']['context_bot_messages']
    groups = [tuple(m['local_id'] for m in ordered[max(0,i-2):i+3])
              for i in (positions[-count:] if count else [])]
    chosen = {m['local_id'] for m in recent} | {i for group in groups for i in group}
    return ContextRows([m for m in ordered if m['local_id'] in chosen],
                       recent_ids=[m['local_id'] for m in recent], bot_groups=groups)


def read_context(cfg, store, chat_id, triggers, *, account=''):
    """Reader-thread-only pagination; at most 1000 raw database records."""
    anchor = source_anchor(triggers)
    cursor = message_key(anchor)
    since = cursor[0]-cfg['target']['context_max_age_seconds']
    watermark = max(m['local_id'] for m in triggers)
    rows, scanned, exhausted = [], 0, False
    required = {m['local_id'] for m in triggers}
    while scanned < SCAN_LIMIT:
        limit = min(PAGE_SIZE, SCAN_LIMIT-scanned)
        page = store.messages_before(chat_id, before=cursor, since=since,
                                     max_local_id=watermark, limit=limit)
        rows.extend(page.messages)
        scanned += page.scanned
        if page.scanned < limit or page.cursor is None:
            exhausted = True
            break
        if page.cursor >= cursor or page.scanned <= 0:
            raise ContextError('invalid_context_cursor')
        cursor = page.cursor
        eligible = sorted((m for m in rows if valid_message(m) and m['local_id'] not in required), key=message_key)
        bots = [i for i,m in enumerate(eligible) if account and m['sender'] == account and not is_pat(m)]
        count = cfg['target']['context_bot_messages'] if account else 0
        if (len(eligible) >= cfg['target']['context_recent_messages']
                and (not count or len(bots) >= count and bots[-count] >= 2)):
            break
    selected = select_context(cfg, rows, anchor, triggers=triggers, account=account)
    selected.scanned = scanned
    selected.scan_limit_hit = scanned == SCAN_LIMIT and not exhausted
    return selected


def context_stats(selection, payload):
    from wxbridge.ai.chat_payload import encode_payload
    required = set(payload['source_ids'])
    return dict(chars=len(encode_payload(payload)), selected=len(selection),
                retained=sum(m['id'] not in required for m in payload['messages']),
                omitted=payload.get('history_omitted', 0), scanned=selection.scanned,
                scan_limit_hit=selection.scan_limit_hit)
