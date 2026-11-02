"""Shared system instructions, prompt composition and source-bound reference slots."""
from __future__ import annotations
from wxbridge.config import InstanceConfig

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from wxbridge import config
from wxbridge.ai.context.selection import ContextError
from wxbridge.ai.context.payload import compact_reference, encode_payload, fit_payload


def system_prompt(cfg: InstanceConfig):
    filename = cfg.system_prompt_file
    if filename:
        path = Path(filename)
        if not path.is_absolute():path = config.PROJECT_ROOT/path
        try: text = path.read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError) as exc:
            raise ContextError(f'无法读取系统提示词文件：{path}（{type(exc).__name__}），取消回复') from exc
        if not text.strip():raise ContextError(f'系统提示词文件为空：{path}，取消回复')
    else: text = '你是群聊助手，请结合上下文回答当前请求。'
    return text.replace('{bot_name}',cfg.target.bot_name or '助手')


@dataclass(frozen=True)
class ReferenceSlot:
    trigger_index: int
    message_id: int
    original: dict = field(repr=False)
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
    """引用媒体绑定前的提示词布局快照，供本地定位、校验和重新裁剪上下文。

    frozen 只禁止字段重新赋值，内部字典仍可修改；绑定时复制后再处理。
    repr=False 的字段不参与对象打印，避免展开账号、聊天正文等内部数据。
    """

    # 机器人账号的 wxid，绑定引用时核对账号归属。
    account: str = field(repr=False)
    # 与 payload 对应的紧凑聊天 JSON 文本，用于定位用户消息中的原始数据区段。
    user_text: str = field(repr=False)
    # 结构化聊天数据快照；绑定引用时复制它，再更新对应消息的 reference。
    payload: dict = field(repr=False)
    # 本轮源消息的引用槽：记录源消息索引、消息 ID、原始引用和来源指纹。
    references: tuple[ReferenceSlot, ...] = field(repr=False)
    # 按顺序保存本轮源消息的 (local_id, sender)，防止引用绑定到其他请求。
    trigger_keys: tuple = field(repr=False)
    # 聊天上下文的字符预算，引用内容更新后仍须满足该上限。
    max_chars: int
    # 超限处理策略（recent / strict），引用更新后的预算检查沿用此策略。
    budget_strategy: str
    # wxid 到成员资料的快照，用其中的 alias 补全引用里的成员称呼。
    aliases: dict | None = field(default=None, repr=False)
    # 历史选择结果及分组、优先级和扫描统计，供预算裁剪和 context_stats 更新使用。
    selection: object = field(default=None, repr=False)


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
        self.context_rules = ''
        self.context_stats = {}


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
        if encode_payload(layout.payload) != layout.user_text:
            raise ContextError('引用媒体登记的数据已改变')
        payload = deepcopy(layout.payload)
        for slot in layout.references:
            if slot.trigger_index >= len(triggers):
                raise ContextError('引用媒体提问映射失效')
            reference = triggers[slot.trigger_index].get('reference')
            if not isinstance(reference, dict):
                raise ContextError('引用媒体来源失效')
            if reference_source(reference) != slot.source:
                raise ContextError('引用媒体来源与登记来源不匹配')
            row = next((r for r in payload['messages'] if r['id']==slot.message_id),None)
            if row is None or row.get('reference') != slot.original:
                raise ContextError('引用媒体内容与登记布局不匹配')
            row['reference'] = compact_reference(reference,account=account,aliases=layout.aliases)
        outside_before, outside_after = text[:offset], text[offset+len(layout.user_text):]
        payload = fit_payload(payload,layout.max_chars,strategy=layout.budget_strategy,
                              external_chars=len(outside_before)+len(outside_after),plan=layout.selection)
        result[1]['content'] = outside_before+encode_payload(payload)+outside_after
        if layout.selection is not None:
            from wxbridge.ai.context.payload import context_stats
            result.context_stats = context_stats(layout.selection,payload)
            result.context_stats['chars'] += len(outside_before)+len(outside_after)
        result.layout = None  # One binding pass; no accidental second patch.
        result.references_bound = True
        return result
