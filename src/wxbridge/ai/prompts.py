"""Persona loading and shared structured chat context with source-bound quotes."""
from copy import deepcopy
from pathlib import Path

from wxbridge import config
from wxbridge.ai.context import ContextError
from wxbridge.ai.chat_context import ChatContext
from wxbridge.ai.chat_payload import CHAT_DATA_RULES
from wxbridge.ai.prompt_parts import PromptLayout, PromptMessages, ReferenceSlot, reference_source


def system_prompt(cfg):
    filename = cfg.get('system_prompt_file')
    if filename:
        path = Path(filename)
        if not path.is_absolute():path = config.PROJECT_ROOT/path
        try:text = path.read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError) as exc:
            raise ContextError(f'无法读取系统提示词文件：{path}（{type(exc).__name__}），取消回复') from exc
        if not text.strip():raise ContextError(f'系统提示词文件为空：{path}，取消回复')
    else:text = '你是群聊助手，请结合上下文回答当前请求。'
    return text.replace('{bot_name}',cfg['target'].get('bot_name') or '助手')


def build_prompt(cfg, context, trigger, chat_title, triggers=None, *, system_text=None, memory=None, account='', profile=None):
    triggers = triggers or [trigger]
    chat = ChatContext.build(cfg, context, triggers, chat_title,
                             account=account, profile=profile, memory=memory).fit(cfg)
    history, value = chat.selection, chat.payload
    limit = cfg['target']['context_max_chars']
    strategy = cfg['target'].get('context_budget_strategy','recent')
    text = chat.text
    slots = tuple(ReferenceSlot(index,m['local_id'],deepcopy(next(row['reference'] for row in value['messages']
                      if row['id']==m['local_id'])),reference_source(m['reference']))
                  for index,m in enumerate(triggers) if isinstance(m.get('reference'),dict))
    prompt = PromptMessages([
        dict(role='system',content=system_prompt(cfg) if system_text is None else system_text),
        dict(role='user',content=text)],
        layout=PromptLayout(account,text,deepcopy(value),slots,
            tuple((m.get('local_id'),m.get('sender')) for m in triggers),limit,strategy,
            deepcopy(profile.get('members',{})) if profile is not None else None, history))
    prompt.context_stats = chat.stats
    chat_kind = '群聊' if cfg['target'].get('chat','').endswith('@chatroom') else '私聊'
    prompt.context_rules = '本轮是微信'+chat_kind+'。'+CHAT_DATA_RULES+(
        '本轮只回应source_ids指定的新消息；多条时合并回应，不把历史发言重新当作提问。'
        '若新消息是拍一拍，可结合当前话题自然回应，不能把事件描述当作发起人的原话。'
        '媒体可读时直接观察，不可读时说明缺少什么；memory不覆盖当前要求，不报分数或心理旁白。')
    return prompt
