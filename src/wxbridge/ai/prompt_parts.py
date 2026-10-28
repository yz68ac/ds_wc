"""Named prompt instructions and source-bound reference slots; no model or I/O."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from wxbridge.ai.context import ContextError
from wxbridge.ai.identity import reference_data


@dataclass(frozen=True)
class ReferenceSlot:
    trigger_index: int
    start: int
    end: int
    original: str = field(repr=False)
    source: str = field(repr=False)


def reference_source(reference):
    """Identity/content invariant; media availability and descriptions may change."""
    import hashlib
    import json
    def identity(item):
        value = {key: item.get(key) for key in ('kind', 'sender', 'server_id', 'image_md5', 'chat_id')}
        if item.get('kind') != 'image':
            value['text'] = item.get('text')
        if isinstance(item.get('reference'), dict):
            value['reference'] = identity(item['reference'])
        return value
    return hashlib.sha256(json.dumps(identity(reference), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class PromptLayout:
    account: str = field(repr=False)
    labels: dict = field(repr=False)
    user_text: str = field(repr=False)
    references: tuple[ReferenceSlot, ...] = field(repr=False)
    trigger_keys: tuple = field(repr=False)
    history_start: int
    history_end: int
    history_lines: tuple[str, ...] = field(repr=False)
    history_removed: int
    memory_suffix: str = field(repr=False)
    memory_intro: str = field(repr=False)
    memory_lines: tuple[str, ...] = field(repr=False)
    max_chars: int
    budget_strategy: str


class PromptMessages(list):
    """Wire-compatible messages with owner-generated assembly metadata.

    Ordinary dict/list consumers and spawned workers remain compatible. Layout
    metadata is never placed into a provider message or the task journal.
    """

    def __init__(self, messages=(), *, layout=None):
        super().__init__(messages)
        self.layout = layout
        self.system_base = None
        self.system_parts = {}
        self.references_bound = False


class PromptComposer:
    @staticmethod
    def instructions(messages, name, text):
        """Add/replace one named policy, preserving data and opaque tool turns."""
        result = deepcopy(messages)
        if not isinstance(result, PromptMessages):
            result = PromptMessages(result)
        if not result or result[0].get('role') != 'system':
            result.insert(0, dict(role='system', content=''))
        current = result[0]['content']
        if result.system_base is None:
            result.system_base = current
        elif current != result.system_base+''.join(result.system_parts.values()):
            # Do not silently erase an unknown edit while replaying old parts.
            raise ContextError('系统提示词已在组装接口之外修改，取消覆盖')
        result.system_parts[name] = text
        result[0]['content'] = result.system_base+''.join(result.system_parts.values())
        return result

    @staticmethod
    def references(messages, triggers, *, account):
        """Patch only verified quote slots; never rebuild history or memory.

        Return None for pre-contract callers. The image adapter retains its
        legacy construction path for those callers during migration.
        """
        import json
        if not isinstance(messages, PromptMessages):
            return None
        if messages.layout is None:
            if messages.references_bound:
                raise ContextError('引用媒体已经绑定，取消重复替换')
            return None
        layout = messages.layout
        if layout.account != account:
            raise ContextError('引用媒体账号与登记布局不匹配')
        if tuple((m.get('local_id'), m.get('sender')) for m in triggers) != layout.trigger_keys:
            raise ContextError('引用媒体提问来源与登记布局不匹配')
        result = deepcopy(messages)
        text = result[1]['content']
        if not isinstance(text, str):
            raise ContextError('引用媒体替换前用户内容已不是纯文本')
        # Outside additions are preserved. Internal edits require recomposing
        # explicitly, rather than guessing where an untrusted quote moved.
        offset = text.find(layout.user_text)
        if offset < 0 or text.find(layout.user_text, offset+1) >= 0:
            raise ContextError('引用媒体提示词布局已改变，取消替换')
        inner = layout.user_text
        updated = []
        for slot in layout.references:
            if slot.trigger_index >= len(triggers):
                raise ContextError('引用媒体提问映射失效')
            reference = triggers[slot.trigger_index].get('reference')
            if not isinstance(reference, dict):
                raise ContextError('引用媒体来源失效')
            if reference_source(reference) != slot.source:
                raise ContextError('引用媒体来源与登记来源不匹配')
            start, end = slot.start, slot.end
            if inner[start:end] != slot.original:
                raise ContextError('引用媒体内容与登记布局不匹配')
            replacement = json.dumps(reference_data(reference, layout.labels, account), ensure_ascii=False)
            updated.append((start, end, replacement))
        for start, end, replacement in reversed(updated):
            inner = inner[:start]+replacement+inner[end:]
        outside_before, outside_after = text[:offset], text[offset+len(layout.user_text):]
        result[1]['content'] = PromptComposer._budget(layout, inner, outside_before, outside_after)
        result.layout = None  # One binding pass; no accidental second patch.
        result.references_bound = True
        return result

    @staticmethod
    def _budget(layout, text, before, after):
        """Media metadata may grow: reduce whole memory lines, then old history."""
        if layout.memory_suffix:
            if not text.endswith(layout.memory_suffix):
                raise ContextError('引用媒体的记忆布局已改变')
            text = text[:-len(layout.memory_suffix)]
        external = len(before)+len(after)
        limit = layout.max_chars
        if limit > 0 and len(text)+external > limit:
            if layout.budget_strategy != 'recent':
                raise ContextError('当前上下文窗口超过字符预算，未截取提问或引用')
            prefix, suffix = text[:layout.history_start], text[layout.history_end:]
            lines, removed = list(layout.history_lines), layout.history_removed
            while lines:
                lines.pop(0)
                removed += 1
                history = (f'（较旧的{removed}条历史因预算未附送；以下不是完整五分钟记录）\n'+'\n'.join(lines)
                           if lines else f'（{removed}条历史因预算未附送）')
                text = prefix+history+suffix
                if len(text)+external <= limit:
                    break
            if len(text)+external > limit:
                raise ContextError('当前提问或明确引用本身超过预算，未截断提问')
        room = limit-len(text)-external-len(layout.memory_intro) if limit > 0 else None
        kept, used = [], 0
        for line in layout.memory_lines:
            cost = len(line)+(1 if kept else 0)
            if room is None or used+cost <= room:
                kept.append(line)
                used += cost
        if kept:
            text += layout.memory_intro+'\n'.join(kept)
        return before+text+after
