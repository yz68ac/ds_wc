"""关系维度的纯数据规则；模型建议须经来源、去重、时间和幅度校验。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re

from wxbridge.ai.emoticons import token_pattern
from wxbridge.storage.expression_memory import clean_text

DAY = 86400
MOOD_TTL = 3*3600
SCENE_GAP = 600
FAMILIARITY_GAP = 1800
MOODS = {'happy': '开心', 'amused': '被逗乐', 'shy': '有点害羞',
         'helpless': '有些无奈', 'upset': '有点不悦', 'neutral': '平静'}
AFFINITY_BANDS = ((20, '明显疏远'), (40, '有些距离'), (60, '普通相处'),
                  (75, '有好感'), (90, '熟悉亲近'), (101, '很亲近'))
PROMPT_POLICY = ('关系只作语气参考，具体偏好和边界优先；尚不熟时保留分寸，高好感不自动暧昧。'
                 '熟悉度仅表示已积累的互动线索，不否认具体共同记忆。'
                 '情绪可随当前语境缓和，不迁怒其他成员；仍明确回应真实@、保证事实准确，避免固定话术。'
                 '不要公开分数、评估或心理过程。')


def defaults():
    return dict(version=1, trust=50, trust_daily={}, trust_evidence=[], familiarity=0,
                familiarity_days={}, familiarity_recent=[], last_scene_at=0, mood=None)


def state(person):
    return person.get('relationship') or defaults()


def identifier(record, dimension):
    return hashlib.sha256((dimension+str(record.get('sources'))+str(record.get('time'))
                           +record.get('reason', '')).encode()).hexdigest()[:16]


def ensure(person):
    changed = 'relationship' not in person
    person.setdefault('relationship', defaults())
    for record in person.get('affinity_evidence', []):
        if 'id' not in record:
            record.update(id=identifier(record, 'affinity'), event=record['reason'],
                          strength='clear', revises=None)
            changed = True
    return changed


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def meaningful(message):
    text = re.sub(token_pattern(), '', message['text']).strip()
    return text if any(c.isalnum() for c in text) else ''


def validate(person):
    if 'relationship' not in person:
        return  # 旧文件通过后按需补齐；不从旧聊天猜测新维度。
    r = person['relationship']
    try:
        valid = (isinstance(r, dict) and set(r) == set(defaults()) and type(r['version']) is int and r['version'] == 1
                 and all(type(r[k]) is int and 0 <= r[k] <= 100 for k in ('trust', 'familiarity'))
                 and finite(r['last_scene_at']))
        if not valid:
            raise ValueError('memory_relationship_invalid_file')
        for key, cap in (('trust_daily', 3), ('familiarity_days', 3)):
            values = r[key]
            if not isinstance(values, dict) or len(values) > 100:
                raise ValueError('memory_relationship_invalid_file')
            for day, number in values.items():
                datetime.strptime(day, '%Y-%m-%d')
                if type(number) is not int or not 0 <= number <= cap:
                    raise ValueError('memory_relationship_invalid_file')
        if not isinstance(r['trust_evidence'], list) or len(r['trust_evidence']) > 30:
            raise ValueError('memory_relationship_invalid_file')
        for item in r['trust_evidence']:
            if (not finite(item['time']) or type(item['delta']) is not int or abs(item['delta']) > 1
                    or not isinstance(item['sources'], list) or not item['sources']
                    or any(type(i) is not int or i < 1 for i in item['sources'])
                    or not re.fullmatch('[0-9a-f]{16}', item['id'])
                    or item['strength'] not in ('weak', 'clear')):
                raise ValueError('memory_relationship_invalid_file')
            clean_text(item['reason'], 160)
            clean_text(item['event'], 80)
        recent = r['familiarity_recent']
        if not isinstance(recent, list) or len(recent) > 90:
            raise ValueError('memory_relationship_invalid_file')
        for item in recent:
            if (not finite(item['time']) or type(item['source']) is not int or item['source'] < 1
                    or not re.fullmatch('[0-9a-f]{64}', item['digest'])):
                raise ValueError('memory_relationship_invalid_file')
        mood = r['mood']
        if mood is not None:
            if (mood['state'] not in MOODS or mood['state'] == 'neutral'
                    or type(mood['intensity']) is not int or mood['intensity'] not in (1, 2)
                    or not finite(mood['time']) or mood['expires_at'] != mood['time']+MOOD_TTL
                    or not isinstance(mood['sources'], list) or not mood['sources']
                    or any(type(i) is not int or i < 1 for i in mood['sources'])):
                raise ValueError('memory_relationship_invalid_file')
            clean_text(mood['reason'], 100)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError('memory_relationship_invalid_file') from exc


def score(person, change, sources, now, *, dimension='affinity'):
    """修改传入副本；返回实际变化。旧三字段协议仍可读，新增协议带事件/证据强度/修订引用。"""
    base, extra = {'delta', 'reason', 'sources'}, {'event', 'strength', 'revises'}
    if not isinstance(change, dict) or set(change) not in (base, base | extra):
        raise ValueError('memory_invalid_'+dimension)
    delta = change['delta']
    cap, day_cap = (3, 10) if dimension == 'affinity' else (1, 3)
    if type(delta) is not int or abs(delta) > cap:
        raise ValueError('memory_'+dimension+'_limit')
    if not delta:
        return 0
    src = sources(change['sources'])
    if not any(meaningful(m) for m in src):
        return 0
    reason = clean_text(change['reason'], 160)
    event = clean_text(change.get('event', reason[:80]), 80)
    strength, revises = change.get('strength', 'clear'), change.get('revises')
    if strength not in ('weak', 'clear') or (revises is not None and not isinstance(revises, str)):
        raise ValueError('memory_relationship_invalid_evidence')
    when = max(m['time'] for m in src)
    if not finite(when) or when > now:
        raise ValueError('memory_relationship_invalid_time')
    ensure(person)
    container = person if dimension == 'affinity' else person['relationship']
    evidence_key = 'affinity_evidence' if dimension == 'affinity' else 'trust_evidence'
    daily_key = 'daily' if dimension == 'affinity' else 'trust_daily'
    evidence = container.get(evidence_key, [])
    if revises is not None:
        target = next((e for e in evidence if e.get('id') == revises), None)
        if (not target or any(e.get('revises') == revises for e in evidence)
                or strength != 'clear' or delta*target['delta'] >= 0 or abs(delta) > abs(target['delta'])):
            raise ValueError('memory_relationship_unknown_revision')
    else:
        recent = [e for e in evidence if when-e['time'] < 7*DAY]
        normalized = lambda text: re.sub(r'\s+', '', text).casefold()
        fingerprints = {hashlib.sha256(normalized(meaningful(m)).encode()).hexdigest() for m in src if meaningful(m)}
        if (any(when-e['time'] < SCENE_GAP for e in recent)
                or any(normalized(e.get('event', e['reason'])) == normalized(event) for e in recent)
                or any(fingerprints.intersection(e.get('fingerprints', [])) for e in recent)):
            return 0
    if strength == 'weak':
        if dimension == 'trust':
            return 0
        delta = max(-1, min(1, delta))
    day = datetime.fromtimestamp(when).date().isoformat()
    daily = container.setdefault(daily_key, {})
    delta = min(abs(delta), max(0, day_cap-daily.get(day, 0)))*(1 if delta > 0 else -1)
    before = container.get(dimension, 50)
    after = max(0, min(100, before+delta))
    container[dimension] = after
    delta = after-before
    if delta:
        daily[day] = daily.get(day, 0)+abs(delta)
        record = dict(delta=delta, reason=reason, event=event, strength=strength, revises=revises,
                      sources=[m['local_id'] for m in src], time=when)
        record['fingerprints'] = sorted({hashlib.sha256(re.sub(r'\s+', '', meaningful(m)).casefold().encode()).hexdigest()
                                         for m in src if meaningful(m)})
        record['id'] = identifier(record, dimension)
        container[evidence_key] = (evidence+[record])[-30:]
    return delta


def apply(person, update, sources, own, now):
    ensure(person)
    r = person['relationship']
    if update is not None:
        if not isinstance(update, dict) or set(update) != {'trust', 'mood'}:
            raise ValueError('memory_relationship_invalid_update')
        score(person, update['trust'], sources, now, dimension='trust')
        mood = update['mood']
        if mood is not None:
            if (not isinstance(mood, dict) or set(mood) != {'state', 'intensity', 'reason', 'sources'}
                    or mood['state'] not in MOODS or type(mood['intensity']) is not int
                    or mood['intensity'] not in (1, 2)):
                raise ValueError('memory_relationship_invalid_mood')
            src = sources(mood['sources'])
            reason = clean_text(mood['reason'], 100)
            when = max(m['time'] for m in src)
            if not finite(when) or when > now:
                raise ValueError('memory_relationship_invalid_time')
            if any(meaningful(m) for m in src) and (not r['mood'] or when >= r['mood']['time']):
                if mood['state'] == 'neutral':
                    r['mood'] = None
                elif when+MOOD_TTL > now:
                    r['mood'] = dict(state=mood['state'], intensity=mood['intensity'], reason=reason,
                                     sources=[m['local_id'] for m in src], time=when, expires_at=when+MOOD_TTL)
    # 熟悉度只根据未处理的新文字计算；模型不能直接给分，也不回扫旧历史。
    for m in sorted(own, key=lambda m: (m['time'], m['local_id'])):
        text = meaningful(m)
        when = m['time']
        if not text or not finite(when) or when > now or when-r['last_scene_at'] < FAMILIARITY_GAP:
            continue
        digest = hashlib.sha256(re.sub(r'\s+', '', text).casefold().encode()).hexdigest()
        if any(e['digest'] == digest and when-e['time'] < 30*DAY for e in r['familiarity_recent']):
            continue
        day = datetime.fromtimestamp(when).date().isoformat()
        used = r['familiarity_days'].get(day, 0)
        gain = min(2 if used == 0 else 1, 3-used, 100-r['familiarity'])
        if gain <= 0:
            continue
        r['familiarity'] += gain
        r['familiarity_days'][day] = used+gain
        r['last_scene_at'] = when
        r['familiarity_recent'] = (r['familiarity_recent']+[dict(digest=digest, time=when, source=m['local_id'])])[-90:]
    prune(person, now)


def prune(person, now):
    changed = ensure(person)
    r = person['relationship']
    for container, key in ((person, 'daily'), (r, 'trust_daily'), (r, 'familiarity_days')):
        old = container.get(key, {})
        kept = {d: n for d, n in old.items() if (datetime.fromtimestamp(now).date()-datetime.fromisoformat(d).date()).days <= 90}
        if kept != old:
            container[key] = kept
            changed = True
    if r['mood'] and now >= r['mood']['expires_at']:
        r['mood'] = None
        changed = True
    kept = [e for e in r['familiarity_recent'] if now-e['time'] < 30*DAY]
    if kept != r['familiarity_recent']:
        r['familiarity_recent'] = kept
        changed = True
    return changed


def background(person, now):
    r = state(person)
    mood = r['mood'] if r['mood'] and now < r['mood']['expires_at'] else None
    return dict(trust=r['trust'], familiarity=r['familiarity'], mood=mood,
                affinity_evidence=person.get('affinity_evidence', [])[-8:], trust_evidence=r['trust_evidence'][-8:])


def describe(person, affinity, now):
    r = state(person)
    band = next(label for end, label in AFFINITY_BANDS if affinity < end)
    familiar = '初识' if r['familiarity'] < 10 else '有些熟悉' if r['familiarity'] < 30 else '熟悉' if r['familiarity'] < 60 else '很熟悉'
    trust = '谨慎信任' if r['trust'] < 30 else '信任普通' if r['trust'] < 70 else '比较信任'
    detail = ('可冷淡短答、少延伸' if affinity < 40 else '自然聊天' if affinity < 60 else
              '愿意接话、适度玩笑' if affinity < 75 else '可嘴硬式关心、熟人调侃' if affinity < 90 else
              '可护短关心、偶尔流露依恋，保持独立')
    text = f'{familiar}，{band}，{trust}；{detail}'
    mood = r['mood']
    if mood and now < mood['expires_at']:
        intensity = '略微' if mood['intensity'] == 1 or now-mood['time'] >= MOOD_TTL/2 else ''
        text += f'；短时{intensity}{MOODS[mood["state"]]}（{mood["reason"]}，不等于长期评价）'
    return text+'。'
