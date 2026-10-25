"""主进程拥有的人物记忆；模型只建议，文件、时间与关系分数由代码控制。"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from wxbridge.ai.avatars import validate_visual, visual_description
from wxbridge.storage import expression_memory, relationships
from wxbridge.ai.emoticons import token_pattern
from wxbridge.storage.memory_selection import MemoryTopic
from wxbridge.storage.member_references import MEMBER_LABEL, ambiguous_fields, bind_text, safe_legacy_metadata

DAY = 86400
LIMITS = {"scene": 10, "event": 10, "preference": 5, "impression": 3}
TTL = {"scene": 6*3600, "event": 30*DAY, "preference": 90*DAY, "impression": 90*DAY}


def ambiguous_labels(entry):
    """Old summaries have no historical label map; never reinterpret it using today's map."""
    return ambiguous_fields(entry, ('summary',))


def usable_entries(person, now):
    return [e for e in active_entries(person, now) if not ambiguous_labels(e)]


def bind_summary(summary, owner, mapping, members, source_texts):
    """Resolve only the current validated snapshot; preserve literal technical identifiers."""
    return bind_text(summary, owner, mapping, members, source_texts)


def private_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("memory_unsafe_path")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077):
        raise ValueError("memory_directory_permissions")
    return path


def validate_existing_directory(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("memory_unsafe_path")
    if path.exists() and (not path.is_dir() or (os.name != "nt" and
            (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077))):
        raise ValueError("memory_directory_permissions")


def atomic_write(path, text):
    fd, temp = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def fuzzy_time(timestamp, now):
    days = (datetime.fromtimestamp(now).date()-datetime.fromtimestamp(timestamp).date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return "这几天"
    if days < 14:
        return "上周"
    return "前阵子"


def affinity_value(person, now):
    score = person.get("affinity", 50)
    anchor = person.get("decay_anchor", person.get("last_interaction", now))
    steps = max(0, int((now-anchor-14*DAY)//(7*DAY)))
    return score + min(steps, 50-score) if score < 50 else score-min(steps, score-50)


def active_entries(person, now):
    return [e for e in person.get("entries", []) if now-e["confirmed_at"] < TTL[e["kind"]]]


class MemoryStore:
    def __init__(self, directory, identity, *, create=True):
        self.identity = dict(identity)
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        root = private_directory(directory) if create else Path(directory).absolute()
        self.directory = private_directory(root/digest) if create else root/digest
        validate_existing_directory(root)
        validate_existing_directory(self.directory)
        self.path = self.directory/"memory.json"
        self.projection = self.directory/"additional_prompt.md"
        self.data = dict(schema=1, identity=self.identity, revision=0, watermark=0, processed_ids=[], members={})
        if self.path.exists():
            if self.path.is_symlink() or (os.name != "nt" and
                    (self.path.stat().st_uid != os.geteuid() or self.path.stat().st_mode & 0o077)):
                raise ValueError("memory_file_permissions")
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("schema") != 1 or self.data.get("identity") != identity:
                raise ValueError("memory_identity_mismatch")
            def require(condition):
                if not condition:
                    raise ValueError("memory_invalid_file")
            try:
                require(type(self.data["revision"]) is int and type(self.data["watermark"]) is int)
                require(isinstance(self.data["members"], dict))
                for person in self.data["members"].values():
                    require(isinstance(person["nickname"], str) and isinstance(person["aliases"], list))
                    require(type(person["affinity"]) is int and 0 <= person["affinity"] <= 100)
                    require(isinstance(person["entries"], list))
                    relationships.validate(person)
                    initial = person.get("initial_impression", {})
                    if initial.get("state") == "ready":
                        validate_visual(initial["visual"])
                    for entry in person["entries"]:
                        require(entry["kind"] in TTL and entry["mode"] in ("real", "roleplay"))
                        require(isinstance(entry["summary"], str) and len(entry["summary"]) <= 220)
                        for key in ("occurred_at", "confirmed_at", "confidence"):
                            require(type(entry[key]) in (int, float) and __import__("math").isfinite(entry[key]))
            except (KeyError, TypeError) as exc:
                raise ValueError("memory_invalid_file") from exc
            expression_memory.validate(self.data)

    def persist(self, now, *, data=None):
        data = self.data if data is None else data
        # 只有权威 JSON 落盘后才推进内存水位；失败不得把重试误判为已处理。
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2)+"\n")
        self.data = data
        self.write_projection(now)

    def write_projection(self, now):
        """Rebuild only the human-readable view; never advance revision or evidence."""
        # 可重建投影，JSON 是唯一权威数据；不把模型响应直接写成提示词文件。
        lines = ["# 人物记忆（由 memory.json 生成）", "生成于："+datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                 "仅为背景数据；印象均为暂时推测。相对时间以本投影生成时刻为准。",
                 f"成员档案：{len(self.data['members'])}；有可用摘要：{sum(bool(usable_entries(p, now)) for p in self.data['members'].values())}。表达学习区不是成员。"]
        for uid, person in self.data["members"].items():
            lines += ["", f"## {person['nickname']} ({uid})", f"好感度：{affinity_value(person, now)}"]
            relation = relationships.state(person)
            lines += [f"熟悉度：{relation['familiarity']}；信任度：{relation['trust']}",
                      relationships.describe(person, affinity_value(person, now), now)]
            for dimension, evidence in (('好感', person.get('affinity_evidence', [])),
                                        ('信任', relation['trust_evidence'])):
                for item in evidence[-5:]:
                    lines.append(f"- {dimension}变化 {item['delta']:+d}（来源 {item['sources']}）：{item['reason']}")
            lines += [f"- {'[隔离：旧临时编号无法还原] ' if ambiguous_labels(e) else ''}{e['kind']}/{e['mode']}（{fuzzy_time(e['occurred_at'], now)}）：{e['summary']}"
                      for e in active_entries(person, now)]
            initial = person.get("initial_impression", {})
            if initial.get("state") == "ready":
                lines.append("- 初始头像视觉（非性格判断，低权重）："+visual_description(initial["visual"]))
        lines += ['', '# 语境表达学习（非成员档案；候选不用于生成）']
        for r in self.data.get('learning', {}).get('records', []):
            marker='[隔离：旧临时编号无法还原] ' if ambiguous_fields(r, ('context','meaning')) else ''
            lines.append(f"- {marker}{r['id']} {r['scope']}/{r['state']} {r['expression']}；语境：{r['context']}；{r['meaning']}")
        try:
            atomic_write(self.projection, "\n".join(lines)+"\n")
        except OSError as exc:
            # 投影可重建；不能让已提交的 JSON 被误回滚或重复计分。
            import logging
            logging.getLogger("wxbridge").warning("记忆投影更新失败 category=%s", type(exc).__name__)

    def status(self, now):
        return dict(revision=self.data["revision"], watermark=self.data["watermark"],
                    members=len(self.data["members"]),
                    entries=sum(len(active_entries(p, now)) for p in self.data["members"].values()),
                    members_with_memories=sum(bool(usable_entries(p, now)) for p in self.data['members'].values()),
                    ambiguous_entries=sum(ambiguous_labels(e) for p in self.data['members'].values() for e in active_entries(p, now)),
                    ambiguous_learning=sum(ambiguous_fields(r, ('context', 'meaning')) for r in expression_memory.active_records(self.data, now)),
                    initial_impressions=sum(p.get("initial_impression", {}).get("state") == "ready" for p in self.data["members"].values()),
                    learning_candidates=sum(r['state'] == 'candidate' for r in self.data.get('learning', {}).get('records', [])),
                    learning_stable=sum(r['state'] == 'stable' for r in self.data.get('learning', {}).get('records', [])))

    def enable_learning(self, cursor, now, enabled=True):
        current = self.data.get('learning')
        if enabled and (current is None or not current.get('enabled', True)):
            data = copy.deepcopy(self.data)
            data.setdefault('learning', dict(started_after_id=cursor, records=[]))
            data['learning'].update(started_after_id=cursor, enabled=True)
            data['revision'] += 1
            self.persist(now, data=data)
        elif not enabled and current and current.get('enabled', True):
            data = copy.deepcopy(self.data)
            data['learning']['enabled'] = False
            data['revision'] += 1
            self.persist(now, data=data)

    def background(self, labels, now):
        result = {}
        for uid, label in labels.items():
            p = self.data["members"].get(uid)
            if p:
                result[label] = dict(nickname=p["nickname"], affinity=affinity_value(p, now),
                                     entries=usable_entries(p, now), relationship=safe_legacy_metadata(relationships.background(p, now)))
        return result

    def prompt(self, labels, asker, now, budget=1200, *, use_avatar=True, dialogue='', use_learning=True, question=''):
        result, used, visual_hints = [], 0, []
        topic = MemoryTopic(dialogue, question)
        order = [asker, *[u for u in labels if u != asker and u in self.data["members"]]]
        for uid in order[:5]:
            p = self.data["members"].get(uid)
            if not p:
                continue
            score = affinity_value(p, now)
            header = f"{labels[uid]}：{safe_legacy_metadata(relationships.describe(p, score, now))}"
            if not result:
                header = relationships.PROMPT_POLICY+'\n'+header
            if used+len(header) > budget:
                continue
            result.append(header)
            used += len(header)+1
            initial = p.get("initial_impression", {})
            entries = usable_entries(p, now)
            # 仅早期弱参考；直接发言形成的印象始终优先，避免头像先入为主。
            if (use_avatar and initial.get("state") == "ready" and now-initial["learned_at"] < 30*DAY
                    and p.get("interaction_batches", 0) < 4 and not any(e["kind"] == "impression" for e in entries)):
                description = visual_description(initial["visual"])
                line = (f"{labels[uid]} 初见视觉线索（弱背景）：{description}。"
                        "不是本人身份、性格或偏好的证据，仅可轻微参考表达；不主动提头像、观察或评价过程，无关就忽略。")
                if description:
                    visual_hints.append(line)
            selected = [(topic.score(e, personal=uid == asker), e) for e in entries]
            for rank, e in sorted(selected, key=lambda item: (-item[0], -item[1]['confirmed_at'], -item[1]['confidence'])):
                if not rank:
                    continue
                label = "暂时推测" if e["kind"] == "impression" else e["kind"]
                line = json.dumps(dict(member=labels[uid], kind=label, mode=e["mode"],
                                       when=fuzzy_time(e["occurred_at"], now), summary=e["summary"]), ensure_ascii=False)
                if used+len(line)+1 <= budget:
                    result.append(line)
                    used += len(line)+1
        if use_learning:
            for line in expression_memory.prompt(self.data, labels, asker, dialogue, budget-used, now):
                result.append(line)
                used += len(line)+1
        # 实际对话记忆和互动偏好先占预算，头像线索只用剩余空间。
        for line in visual_hints:
            if used+len(line)+1 <= budget:
                result.append(line)
                used += len(line)+1
        return "\n".join(result)

    def apply(self, proposal, snapshot, now):
        """全量先校验再提交；来源必须是本窗口该成员的新直接发言。"""
        if snapshot["identity"] != self.identity or snapshot["revision"] != self.data["revision"]:
            raise ValueError("memory_stale_revision")
        if snapshot["watermark"] != self.data["watermark"]:
            raise ValueError("memory_stale_watermark")
        if (not isinstance(proposal, dict) or 'updates' not in proposal or
                set(proposal)-{'updates', 'avatar_updates', 'learning'} or not isinstance(proposal['updates'], list)):
            raise ValueError("memory_invalid_schema")
        if len(proposal["updates"]) > len(snapshot["members"]):
            raise ValueError("memory_invalid_members")
        mapping = {v["label"]: uid for uid, v in snapshot["members"].items()}
        processed = set(self.data.get("processed_ids", []))
        evidence = {m["id"]: m for m in snapshot["messages"] if m["local_id"] > self.data["watermark"]
                    and m["local_id"] not in processed}
        interactions = [m for m in snapshot.get("interactions", []) if m.get("kind") == "pat"
                        and m["local_id"] > self.data["watermark"] and m["local_id"] not in processed]
        data, seen, scored = copy.deepcopy(self.data), set(), set()
        def sources(value, label):
            if not isinstance(value, list) or not value or len(value) > 30 or len(set(value)) != len(value):
                raise ValueError("memory_invalid_sources")
            if any(s not in evidence or evidence[s]["member"] != label for s in value):
                raise ValueError("memory_unknown_source")
            return [evidence[s] for s in value]
        def text(value, limit):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("memory_invalid_text")
            if re.search(r"sk-[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", value):
                raise ValueError("memory_sensitive_text")
            return value.strip()
        unbound=object()
        def bound_change(value, label, owner, fields=('reason', 'event')):
            if not isinstance(value, dict):
                return value
            result=copy.deepcopy(value)
            for field in fields:
                content=result.get(field)
                if not isinstance(content,str) or not MEMBER_LABEL.search(content):
                    continue
                proof=sources(result.get('sources'),label)
                try:
                    result[field],_=bind_text(content,owner,mapping,snapshot['members'],[m['text'] for m in proof])
                except ValueError:
                    data['rejected_label_metadata']=data.get('rejected_label_metadata',0)+1
                    return unbound
            return result
        for update in proposal["updates"]:
            if (not isinstance(update, dict) or not {'member', 'memories', 'affinity'} <= set(update)
                    or set(update)-{'member', 'memories', 'affinity', 'relationship'}):
                raise ValueError("memory_invalid_update")
            label = update["member"]
            if label not in mapping or label in seen:
                raise ValueError("memory_unknown_member")
            if not any(m['member'] == label for m in evidence.values()):
                # 模型可为只出现在旧对白中的成员返回空占位；它不产生记忆或关系变化。
                def zero(value):
                    return (isinstance(value, dict) and type(value.get('delta')) is int and value['delta'] == 0
                            and value.get('sources') == [] and value.get('reason') == '')
                extra = update.get('relationship')
                if (update['memories'] == [] and zero(update['affinity'])
                        and (extra is None or (isinstance(extra, dict) and set(extra) == {'trust', 'mood'}
                                               and zero(extra['trust']) and extra['mood'] is None))):
                    seen.add(label)
                    continue
                raise ValueError('memory_unknown_source')
            seen.add(label)
            uid = mapping[label]
            person = data["members"].setdefault(uid, dict(nickname="", aliases=[], entries=[], affinity=50,
                last_interaction=now, decay_anchor=now, daily={}, affinity_evidence=[]))
            memories = update["memories"]
            if not isinstance(memories, list) or len(memories) > 23:
                raise ValueError("memory_invalid_entries")
            person["entries"] = active_entries(person, now)
            for item in memories:
                if not isinstance(item, dict) or set(item) != {"kind", "mode", "summary", "sources", "confidence", "replaces"}:
                    raise ValueError("memory_invalid_entry")
                kind, mode = item["kind"], item["mode"]
                if kind not in TTL or mode not in ("real", "roleplay") or (kind == "impression" and mode != "real"):
                    raise ValueError("memory_invalid_kind")
                summary = text(item["summary"], 220)
                confidence = item["confidence"]
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                    raise ValueError("memory_invalid_confidence")
                src = sources(item["sources"], label)
                try:
                    summary, literal_labels = bind_summary(summary, uid, mapping, snapshot['members'], [m['text'] for m in src])
                    summary = text(summary, 220)
                except ValueError as exc:
                    if str(exc) != 'memory_unbound_label':
                        raise
                    # Reject only this ambiguous suggestion; other validated updates still progress.
                    data['rejected_label_summaries'] = data.get('rejected_label_summaries', 0)+1
                    continue
                if kind == 'impression' and all(not re.sub(token_pattern(), '', m['text']).strip() for m in src):
                    continue
                previous = next((e for e in person["entries"] if e["id"] == item["replaces"]), None)
                if item["replaces"] is not None and (not previous or previous["kind"] != kind or previous["mode"] != mode):
                    raise ValueError("memory_invalid_replacement")
                entry = dict(id=hashlib.sha256((uid+kind+mode+summary+str(src[-1]["local_id"])).encode()).hexdigest()[:16],
                             kind=kind, mode=mode, summary=summary, confidence=float(confidence),
                             sources=[m["local_id"] for m in src],
                             occurred_at=previous["occurred_at"] if previous else min(m["time"] for m in src),
                             confirmed_at=max(m["time"] for m in src))
                if literal_labels:
                    entry['literal_labels'] = literal_labels
                person["entries"] = [e for e in person["entries"] if e != previous and
                                     (e["kind"], e["mode"], e["summary"]) != (kind, mode, summary)] + [entry]
            # 自然回归只结算一次，然后交给独立规则校验本轮增量。
            person['affinity'] = affinity_value(person, now)
            affinity=bound_change(update['affinity'],label,uid)
            relationships.score(person, affinity if affinity is not unbound else dict(delta=0,reason='',sources=[]), lambda ids: sources(ids, label), now)
            scored.add(uid)
            relation=copy.deepcopy(update.get('relationship'))
            if isinstance(relation,dict):
                if 'trust' in relation:
                    trust=bound_change(relation['trust'],label,uid)
                    relation['trust']=trust if trust is not unbound else dict(delta=0,reason='',sources=[])
                if 'mood' in relation:
                    mood=bound_change(relation['mood'],label,uid,('reason',))
                    relation['mood']=mood if mood is not unbound else None
            relationships.apply(person, relation, lambda ids: sources(ids, label), [], now)
        # 真实互动可更新昵称与互动时间，但拍一拍不属于发言证据，不参与记事/好感计分。
        for uid, member in snapshot["members"].items():
            own = [m for m in [*evidence.values(), *interactions] if m["member"] == member["label"]]
            if not own:
                continue
            p = data["members"].setdefault(uid, dict(nickname="", aliases=[], entries=[], affinity=50,
                last_interaction=now, decay_anchor=now, daily={}, affinity_evidence=[]))
            if p["nickname"] != member["nickname"]:
                p["aliases"] = ([n for n in p["aliases"] if n != member["nickname"]]+([p["nickname"]] if p["nickname"] else []))[-5:]
                p["nickname"] = member["nickname"]
            # 先结算旧的自然回归，再重新设置互动时钟。
            if uid not in scored:
                p["affinity"] = affinity_value(p, now)
            p["last_interaction"] = max(p.get('last_interaction', 0), max(m["time"] for m in own))
            p["decay_anchor"] = p["last_interaction"]
            p["interaction_batches"] = p.get("interaction_batches", 0)+1
            p.setdefault("initial_impression", dict(state="pending", checked_at=0, attempts=0))
            own_text = [m for m in evidence.values() if m['member'] == member['label']]
            relationships.apply(p, None, None, own_text, now)
            kept = active_entries(p, now)
            p["entries"] = []
            for kind, limit in LIMITS.items():
                p["entries"] += sorted((e for e in kept if e["kind"] == kind),
                                        key=lambda e: (e["confidence"], e["confirmed_at"]), reverse=True)[:limit]
        for p in data["members"].values():
            p["entries"] = active_entries(p, now)
        expression_memory.apply(data, proposal.get('learning', []), snapshot, now)
        self._apply_avatars(data, proposal.get("avatar_updates", []), snapshot, now)
        data["watermark"] = max(data["watermark"], snapshot["through"])
        # 微信可能晚同步一条发送时间较早、local_id 却比触发大的消息。
        # 记录水位之外已处理来源，既不重复计分，也不跳过较小 ID 的后续触发。
        data["processed_ids"] = sorted(i for i in processed | {m["local_id"] for m in [*evidence.values(), *interactions]}
                                       if i > data["watermark"])
        data["revision"] += 1
        self.persist(now, data=data)

    def _apply_avatars(self, data, updates, snapshot, now):
        bindings = snapshot.get("avatars", {})
        mapping = {v["label"]: uid for uid, v in snapshot["members"].items()}
        valid = {}
        try:
            if not isinstance(updates, list) or len(updates) > len(bindings):
                raise ValueError("avatar_invalid_result")
            for update in updates:
                if not isinstance(update, dict) or set(update) != {"member", "digest", "visual"}:
                    raise ValueError("avatar_invalid_result")
                uid = mapping.get(update["member"])
                binding = bindings.get(uid, {})
                if (uid in valid or not binding.get("available") or update["digest"] != binding["digest"]
                        or hashlib.sha256(binding["data"]).hexdigest() != binding["digest"]):
                    raise ValueError("avatar_unknown_source")
                valid[uid] = validate_visual(update["visual"])
        except (ValueError, KeyError, TypeError):
            valid = {}  # 头像建议不合格时仍提交独立的文本记忆，不写入未知来源。
        for uid, binding in bindings.items():
            if uid not in snapshot["members"] or uid not in data["members"]:
                continue
            p = data["members"][uid]
            initial = p.get("initial_impression", {})
            if initial.get("state") in ("ready", "forgotten", "expired"):
                continue
            attempts = initial.get("attempts", 0)+1
            if uid in valid:
                p["initial_impression"] = dict(state="ready", origin="avatar_visual", confidence=0.2,
                    learned_at=now, checked_at=now, attempts=attempts, digest=binding["digest"],
                    observed_at=binding["observed_at"], visual=valid[uid])
            else:
                code = binding.get("error")
                if code not in {"avatar_invalid_member", "avatar_not_cached", "avatar_ambiguous", "avatar_size_limit", "avatar_unsupported", "avatar_unavailable"}:
                    code = "avatar_analysis_unavailable"
                p["initial_impression"] = dict(state="unavailable", checked_at=now, attempts=attempts, error=code)

    def maintain(self, action, *, member=None, entry=None, text=None, watermark=0, now, scope=None):
        matches = [r for r in self.data.get('learning', {}).get('records', [])
                   if (scope is None or r['scope'] == scope) and (member is None or r['owner'] == member)
                   and (entry is None or r['id'] == entry)]
        if scope in ('group', 'bot') or (entry is not None and matches):
            data = copy.deepcopy(self.data)
            if action in ('clear', 'forget'):
                if action == 'forget' and entry and not matches:
                    raise ValueError('memory_entry_not_found')
                expression_memory.forget(data, scope=scope, member=member, entry=entry, now=now)
            elif action == 'correct' and entry and len(matches) == 1:
                replacement = expression_memory.clean_text(text, 120)
                record = next(r for r in data['learning']['records'] if r['id'] == entry)
                record.update(meaning=replacement, corrected_at=now, confirmed_at=now)
                if record['scope'] == 'group':
                    data['learning']['records'] = [r for r in data['learning']['records'] if r.get('basis') != entry]
            else:
                raise ValueError('memory_invalid_action')
            data['watermark'] = max(data['watermark'], watermark)
            data['revision'] += 1
            self.persist(now, data=data)
            return
        if action == "clear":
            self.data["members"] = {}
            if 'learning' in self.data:
                self.data['learning']['records'] = []
        elif action == "forget":
            person = self.data["members"].get(member)
            if not person:
                raise ValueError("memory_member_not_found")
            if entry:
                if entry == 'relationship':
                    person.update(affinity=50, daily={}, affinity_evidence=[], decay_anchor=now,
                                  relationship=relationships.defaults())
                    self.data['watermark'] = max(self.data['watermark'], watermark)
                    self.data['revision'] += 1
                    self.persist(now)
                    return
                if entry == "initial":
                    person["initial_impression"] = dict(state="forgotten", checked_at=now)
                    self.data["watermark"] = max(self.data["watermark"], watermark)
                    self.data["revision"] += 1
                    self.persist(now)
                    return
                if not any(e["id"] == entry for e in person["entries"]):
                    raise ValueError("memory_entry_not_found")
                person["entries"] = [e for e in person["entries"] if e["id"] != entry]
            else:
                del self.data["members"][member]
                expression_memory.forget(self.data, member=member, now=now)
        elif action == "correct":
            if not isinstance(text, str) or not text.strip() or len(text) > 220:
                raise ValueError("memory_invalid_text")
            person = self.data["members"].get(member, {})
            record = next((e for e in person.get("entries", []) if e["id"] == entry), None)
            if not record:
                raise ValueError("memory_entry_not_found")
            record.update(summary=text.strip(), corrected_at=now)
        else:
            raise ValueError("memory_invalid_action")
        self.data["watermark"] = max(self.data["watermark"], watermark)
        self.data["revision"] += 1
        self.persist(now)

    def prune(self, now):
        changed = False
        data = copy.deepcopy(self.data)
        for person in data["members"].values():
            changed = relationships.prune(person, now) or changed
            entries = active_entries(person, now)
            if entries != person["entries"]:
                person["entries"] = entries
                changed = True
            initial = person.get("initial_impression", {})
            if initial.get("state") == "ready" and now-initial["learned_at"] >= 90*DAY:
                person["initial_impression"] = dict(state="expired", checked_at=initial["checked_at"])
                changed = True
        changed = expression_memory.prune(data, now) or changed
        if changed:
            data["revision"] += 1
            self.persist(now, data=data)
        return changed
