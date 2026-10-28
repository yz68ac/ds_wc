"""Memory-to-text presentation, independent of persistence and model clients."""
import json
from wxbridge.storage import relationships
from wxbridge.storage.memory_rules import DAY, affinity_value, usable_entries, fuzzy_time
from wxbridge.storage.memory_selection import MemoryTopic
from wxbridge.storage.member_references import safe_legacy_metadata
from wxbridge.storage.visuals import visual_description


def render_memory(view, labels, asker, now, budget=1200, *, use_avatar=True, dialogue='', use_learning=True, question=''):
    result, used, visual_hints = [], 0, []
    topic = MemoryTopic(dialogue, question)
    order = [asker, *[u for u in labels if u != asker and u in view.members]]
    for uid in order[:5]:
        p = view.members.get(uid)
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
            description = visual_description(dict(initial["visual"]))
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
        for line in view.expression_prompt(labels, asker, dialogue, budget-used, now):
            result.append(line)
            used += len(line)+1
    # 实际对话记忆和互动偏好先占预算，头像线索只用剩余空间。
    for line in visual_hints:
        if used+len(line)+1 <= budget:
            result.append(line)
            used += len(line)+1
    return "\n".join(result)
