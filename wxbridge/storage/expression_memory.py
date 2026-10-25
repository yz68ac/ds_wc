"""语境表达证据的确定性校验、跨天确认与遗忘；不调用模型或写文件。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import math

from wxbridge.ai.emoticons import is_token

DAY = 86400


def clean_text(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or re.search(r'sk-[A-Za-z0-9]{16,}|-----BEGIN .*PRIVATE KEY-----', value)):
        raise ValueError('memory_learning_invalid_text')
    return value.strip()


def state(record):
    evidence = sorted(record.get('evidence', []), key=lambda e: (e['time'], e['id']))
    explicit = [e for e in evidence if e['stance'] in ('explicit', 'reject')]
    if explicit and record['scope'] == 'member':
        return 'blocked' if explicit[-1]['stance'] == 'reject' else 'stable'
    boundary = max((e['time'] for e in evidence if e['stance'] == 'counter'), default=-1)
    observations = []
    for e in evidence:
        if e['stance'] != 'support' or e['time'] <= boundary:
            continue
        if not observations or e['time']-observations[-1]['time'] >= 600:
            observations.append(e)
    dates = {datetime.fromtimestamp(e['time']).date() for e in observations}
    needed = 3 if record['scope'] == 'member' else 5
    members = len({e['member'] for e in evidence if e['stance'] == 'support' and e['time'] > boundary})
    return 'stable' if len(observations) >= needed and len(dates) >= 2 and (record['scope'] == 'member' or members >= 3) else 'candidate'


def prune(data, now):
    learning = data.get('learning')
    if not learning:
        return False
    old = learning.get('records', [])
    kept = []
    for record in old:
        ttl = 7*DAY if record['state'] == 'candidate' else (90 if record['scope'] == 'member' else 30)*DAY
        if now-record['confirmed_at'] < ttl:
            kept.append(record)
    kept.sort(key=lambda r: (r['state'] == 'blocked', r['state'] == 'stable', r['confirmed_at']), reverse=True)
    selected, counts, candidates = [], {}, 0
    for record in kept:
        if record['state'] == 'candidate':
            if candidates >= 40:
                continue
            candidates += 1
        else:
            key = (record['scope'], record['owner'])
            limit = 10 if record['scope'] == 'group' else 5
            if counts.get(key, 0) >= limit:
                continue
            counts[key] = counts.get(key, 0)+1
        selected.append(record)
    group_ids = {r['id'] for r in selected if r['scope'] == 'group' and r['state'] == 'stable'}
    selected = [r for r in selected if r['scope'] != 'bot' or r.get('basis') in group_ids]
    learning['records'] = selected
    return selected != old


def validate(data):
    if 'learning' not in data:
        return
    learning = data['learning']
    def require(condition):
        if not condition:
            raise ValueError('memory_learning_invalid_file')
    try:
        require(type(learning['started_after_id']) is int and learning['started_after_id'] >= 0)
        require(isinstance(learning['records'], list))
        seen = set()
        for r in learning['records']:
            require(isinstance(r['id'], str) and r['id'] not in seen)
            seen.add(r['id'])
            require(r['scope'] in ('member', 'group', 'bot') and r['state'] in ('candidate', 'stable', 'blocked'))
            require(isinstance(r['owner'], str) and (r['scope'] != 'member' or r['owner'] in data['members']))
            for key, limit in (('expression', 24), ('context', 80), ('meaning', 120)):
                clean_text(r[key], limit)
            require(all(type(r[key]) in (int, float) and math.isfinite(r[key]) for key in ('occurred_at', 'confirmed_at')))
            require(isinstance(r['evidence'], list) and len(r['evidence']) <= 30)
            for e in r['evidence']:
                require(type(e['id']) is int and isinstance(e['member'], str))
                require(type(e['time']) in (int, float) and math.isfinite(e['time']))
                require(e['stance'] in ('support', 'counter', 'explicit', 'reject'))
    except (KeyError, TypeError) as error:
        raise ValueError('memory_learning_invalid_file') from error


def active_records(data, now):
    records = [r for r in data.get('learning', {}).get('records', [])
               if now-r['confirmed_at'] < (7*DAY if r['state'] == 'candidate' else (90 if r['scope'] == 'member' else 30)*DAY)]
    bases = {r['id'] for r in records if r['scope'] == 'group' and r['state'] == 'stable'}
    return [r for r in records if r['scope'] != 'bot' or r.get('basis') in bases]


def apply(data, proposals, snapshot, now):
    from wxbridge.storage.member_references import bind_text
    if not isinstance(proposals, list) or len(proposals) > 3:
        raise ValueError('memory_learning_limit')
    if not proposals:
        return
    if not snapshot.get('learning_enabled', False):
        raise ValueError('memory_learning_disabled')
    learning = data.setdefault('learning', dict(started_after_id=0, records=[]))
    floor = max(data['watermark'], learning['started_after_id'])
    processed = set(data.get('processed_ids', []))
    sources = {m['id']: m for m in snapshot['messages'] if m['local_id'] > floor and m['local_id'] not in processed}
    members = {v['label']: uid for uid, v in snapshot['members'].items()}
    records = learning['records']
    # 群内成熟记录先于其衍生的机器人偏好校验。
    for item in sorted(proposals, key=lambda x: x.get('scope') == 'bot' if isinstance(x, dict) else False):
        fields = {'scope', 'member', 'expression', 'context', 'meaning', 'sources', 'stance', 'replaces', 'basis'}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('memory_learning_invalid_schema')
        scope, stance = item['scope'], item['stance']
        if scope not in ('member', 'group', 'bot') or stance not in ('support', 'counter', 'explicit', 'reject'):
            raise ValueError('memory_learning_invalid_scope')
        owner = members.get(item['member']) if scope == 'member' else ''
        if (scope == 'member' and not owner) or (scope != 'member' and item['member'] is not None):
            raise ValueError('memory_learning_unknown_member')
        if stance in ('explicit', 'reject') and scope != 'member':
            raise ValueError('memory_learning_explicit_scope')
        expression = clean_text(item['expression'], 24)
        context, meaning = clean_text(item['context'], 80), clean_text(item['meaning'], 120)
        ids = item['sources']
        if (not isinstance(ids, list) or not ids or len(ids) > 10 or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or any(i not in sources for i in ids)):
            raise ValueError('memory_learning_unknown_source')
        evidence = [sources[i] for i in ids]
        if scope == 'member' and any(members.get(m['member']) != owner for m in evidence):
            raise ValueError('memory_learning_foreign_source')
        try:
            context,context_labels=bind_text(context,owner,members,snapshot['members'],[m['text'] for m in evidence])
            meaning,meaning_labels=bind_text(meaning,owner,members,snapshot['members'],[m['text'] for m in evidence])
            context,meaning=clean_text(context,80),clean_text(meaning,120)
        except ValueError as exc:
            if str(exc)!='memory_unbound_label':raise
            data['rejected_label_learning']=data.get('rejected_label_learning',0)+1
            continue
        dialogue = {m['id']: m for m in snapshot.get('dialogue', [])}
        for m in evidence:
            if expression in m['text']:
                continue
            # 明确反馈可以指向先前机器人表达，但发言人必须仍是本人新发言。
            contextual = any(row['id'] != m['id'] and expression in row['text'] and row['time'] <= m['time']
                             and m['time']-row['time'] <= 120 for row in dialogue.values())
            if stance == 'support' or not contextual:
                raise ValueError('memory_learning_expression_not_observed')
        previous = next((r for r in records if r['id'] == item['replaces']), None)
        if item['replaces'] is not None and (previous is None or (previous['scope'], previous['owner'], previous['expression']) != (scope, owner, expression)):
            raise ValueError('memory_learning_invalid_replacement')
        if previous is None:
            previous = next((r for r in records if (r['scope'], r['owner'], r['expression'], r['context']) == (scope, owner, expression, context)), None)
        if scope == 'bot':
            basis = next((r for r in records if r['id'] == item['basis'] and r['scope'] == 'group'
                          and r['state'] == 'stable' and r['expression'] == expression), None)
            if not basis or (expression.startswith('[') and expression.endswith(']') and not is_token(expression)):
                raise ValueError('memory_learning_invalid_basis')
        elif item['basis'] is not None:
            raise ValueError('memory_learning_invalid_basis')
        record = previous
        if record is None:
            identifier = hashlib.sha256(json.dumps([scope, owner, expression, context], ensure_ascii=False).encode()).hexdigest()[:16]
            record = dict(id=identifier, scope=scope, owner=owner, expression=expression, context=context,
                          meaning=meaning, evidence=[], state='candidate', basis=item['basis'], occurred_at=min(m['time'] for m in evidence))
            records.append(record)
        existing_ids = {e['id'] for e in record['evidence']}
        fresh = [m for m in evidence if m['local_id'] not in existing_ids]
        if stance == 'support':
            independent = []
            observed = list(record['evidence'])
            for m in sorted(fresh, key=lambda m: (m['time'], m['local_id'])):
                uid = members[m['member']]
                if any(e['stance'] == 'support' and e['member'] == uid and abs(m['time']-e['time']) < 600 for e in observed):
                    continue
                independent.append(m)
                observed.append(dict(time=m['time'], member=uid, stance=stance))
            fresh = independent
        if not fresh:
            continue
        record['evidence'] = sorted(record['evidence']+[dict(id=m['local_id'], time=m['time'], member=members[m['member']], stance=stance)
                                                       for m in fresh], key=lambda e: (e['time'], e['id']))[-30:]
        record.update(confirmed_at=max(e['time'] for e in record['evidence']), context=context, basis=item['basis'])
        record['literal_labels']=sorted(set(context_labels+(meaning_labels if stance!='counter' else record.get('literal_labels',[]))))
        if stance != 'counter':
            record['meaning'] = meaning
        record['state'] = ('stable' if scope == 'bot' and stance == 'support' else state(record))
    prune(data, now)


def background(data, labels, now):
    from wxbridge.storage.member_references import ambiguous_fields
    mapping = {uid: label for uid, label in labels.items()}
    result = []
    for record in active_records(data, now):
        if ambiguous_fields(record,('context','meaning')):
            continue
        if record['scope'] == 'member' and record['owner'] not in labels:
            continue
        item = {key: record[key] for key in ('id', 'scope', 'expression', 'context', 'meaning', 'state', 'basis')}
        item['member'] = mapping.get(record['owner'])
        item['evidence_count'] = len(record['evidence'])
        item['evidence_days'] = len({datetime.fromtimestamp(e['time']).date() for e in record['evidence']})
        result.append(item)
    return result


def prompt(data, labels, asker, dialogue, budget, now):
    from wxbridge.storage.member_references import ambiguous_fields
    relevant = []
    for record in active_records(data, now):
        if ambiguous_fields(record,('context','meaning')):
            continue
        if record['state'] == 'candidate' or (record['scope'] == 'member' and record['owner'] not in labels):
            continue
        if record['scope'] != 'member' and record['expression'] not in dialogue and not any(
                token in dialogue for token in re.findall(r'[\u3400-\u9fff]{2,}|[A-Za-z]{3,}', record['context'])):
            continue
        # 优先当前提问者及当轮已经出现的表达。
        rank = (record['state'] == 'blocked', record['owner'] == asker,
                record['expression'] in dialogue, record['confirmed_at'])
        member = labels.get(record['owner'])
        text = json.dumps(dict(scope=record['scope'], member=member, expression=record['expression'],
                               when=record['context'], use='避免使用' if record['state'] == 'blocked' else record['meaning']), ensure_ascii=False)
        relevant.append((rank, text))
    result = []
    for _, text in sorted(relevant, reverse=True):
        if len(result) == 2:
            break
        line = '表达习惯弱背景（仅语境适合时参考，不复读、不覆盖人格或表情开关）：'+text
        if sum(len(s)+1 for s in result)+len(line)+1 <= min(300, budget):
            result.append(line)
    return result


def forget(data, *, scope=None, member=None, entry=None, now):
    learning = data.get('learning')
    if not learning:
        return
    records = learning['records']
    kept = []
    for r in records:
        selected = (scope in (None, r['scope']) and (member is None or r['owner'] == member)
                    and (entry is None or r['id'] == entry))
        if selected:
            continue
        if member and entry is None:
            r['evidence'] = [e for e in r['evidence'] if e['member'] != member]
            if not r['evidence']:
                continue
            if r['scope'] != 'bot':
                r['state'] = state(r)
        kept.append(r)
    learning['records'] = kept
    prune(data, now)
