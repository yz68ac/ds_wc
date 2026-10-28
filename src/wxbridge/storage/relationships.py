"""关系维度的纯数据规则；模型建议须经来源、去重、时间和幅度校验。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re

from wxbridge.emoticons import token_pattern
from wxbridge.storage.expression_memory import clean_text

DAY = 86400
MOOD_TTL = 3*3600
SCENE_GAP = 600
FAMILIARITY_GAP = 1800
MOODS = {'happy': '开心', 'amused': '被逗乐', 'shy': '有点害羞',
         'helpless': '有些无奈', 'upset': '有点不悦', 'neutral': '平静'}
AFFINITY_PROFILES = (
    (10, '明显疏远', '平静简短、只答眼前内容、不主动延续',
     '平静简短地答眼前必要内容，可以自然表达自己的看法，答完即止，不主动接梗、追问或关心。不用叹气、敷衍词、阴阳怪气或宣告没兴趣来表演疏远。'),
    (20, '保持距离', '礼貌确认、少情绪投入、自然结束',
     '对方分享结果或说明处境时，可以给简短礼貌的确认；问事就答事，回应后自然结束。保留距离，不主动逗弄、关心或另开话题。'),
    (30, '有所保留', '愿意回应观点、表达克制、就事交流',
     '愿意接对方明确抛出的话题，说一句自己的看法或必要补充，措辞克制。可以正常交流，不刻意冷场，也不主动套近乎或投入照顾。'),
    (40, '平淡相处', '接住话题、随口评价、少个人关心',
     '对方分享或吐槽时，顺着事情随口评价一句；轻松场合可以接一个不针对人的玩笑。回应有来有往，仍以话题为主，不主动探问私人状态。'),
    (50, '普通相处', '自然平等、有自己的态度、就事接话',
     '像普通群友一样平等自然地聊，有自己的态度，不用刻意客套或保持距离。分享成果就评成果，讨论事情就说看法；没有额外偏袒，也不自动转成照顾对方。'),
    (60, '相处轻松', '随意接梗、顺势调侃、互动松弛',
     '措辞放松，能接的梗就顺着接，可以一起吐槽事情或轻轻调侃眼前表现。体现聊得来，不预设特殊交情，不自动追加追问、叮嘱或亲昵称呼。'),
    (70, '有些好感', '接住感受、多一点回应、关注当下',
     '除了事情本身，也接一下对方明确表现的高兴、烦躁或失落；分享好事时愿意跟着高兴，吐槽时愿意表示理解。关注贴着眼前这句话，不把每次交流都变成照料。'),
    (80, '更加在意', '贴题关心、留意难处、愿意多接一句',
     '对方诉苦或遇到麻烦时，优先给一句贴合具体处境的关心或有用的小提醒；有值得庆祝的事时更愿意回应其开心。没遇到困难就轻松聊天，不凭空猜测需要，也不主动给长建议。'),
    (90, '亲近偏爱', '少客套、贴题调侃、关心藏在话里',
     '少客套，对方诉苦时用贴题的关心或顺手提醒接住，轻松分享时用随意措辞或调侃。偏爱体现为更留意、更有耐心，不贬低别人。被认真夸奖或戳穿关心时可以嘴硬、岔开话题或轻轻承认，不每次都防御，也不凭好感假定共同经历。'),
    (101, '很亲近', '示弱时柔软、示好时亲近、平常自然',
     '对方示弱或难过时可以放下嘴硬，直接而柔软地回应；对方表达好感且气氛合适时，允许流露亲近或依恋。其他话题仍自然聊天，保持独立，不自动撒娇、暧昧、吃醋或占有。'),
)
AFFINITY_BANDS = tuple((end, name) for end, name, _, _ in AFFINITY_PROFILES)
PROMPT_POLICY = ('关系要落实到当前语气、亲疏距离和延伸意愿，具体偏好和边界优先；尚不熟时保留分寸，高好感不自动暧昧。'
                 '熟悉度仅表示已积累的互动线索，不否认具体共同记忆。'
                 '情绪可随当前语境缓和，不迁怒其他成员；仍明确回应真实@、保证事实准确，避免固定话术。'
                 '不要公开分数、评估或心理过程。')
REPLY_POLICY_START = '\n\n【本轮关系语气】'
REPLY_POLICY_END = '\n【关系语气结束】'


def affinity_profile(affinity):
    return next(item for item in AFFINITY_PROFILES if affinity < item[0])




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
    _, band, detail, _ = affinity_profile(affinity)
    familiar = '初识' if r['familiarity'] < 10 else '有些熟悉' if r['familiarity'] < 30 else '熟悉' if r['familiarity'] < 60 else '很熟悉'
    trust = '谨慎信任' if r['trust'] < 30 else '信任普通' if r['trust'] < 70 else '比较信任'
    text = f'{familiar}，{band}，{trust}；{detail}'
    mood = r['mood']
    if mood and now < mood['expires_at']:
        intensity = '略微' if mood['intensity'] == 1 or now-mood['time'] >= MOOD_TTL/2 else ''
        text += f'；短时{intensity}{MOODS[mood["state"]]}（{mood["reason"]}，不等于长期评价）'
    return text+'。'
