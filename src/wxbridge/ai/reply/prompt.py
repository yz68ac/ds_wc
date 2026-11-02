"""Reply prompt assembly with shared chat context and source-bound quotes."""
from wxbridge.config import InstanceConfig
from copy import deepcopy

from wxbridge.ai.context.chat import ChatContext
from wxbridge.ai.context.payload import CHAT_DATA_RULES
from wxbridge.ai.composer import PromptLayout, PromptMessages, ReferenceSlot, reference_source, system_prompt


def build_prompt(cfg: InstanceConfig, context, trigger, chat_title, triggers=None, *, system_text=None, memory=None, account='', profile=None):
    triggers = triggers or [trigger]
    chat = ChatContext.build(cfg, context, triggers, chat_title,
                             account=account, profile=profile, memory=memory).fit(cfg)
    history, value = chat.selection, chat.payload
    limit = cfg.target.context_max_chars
    strategy = cfg.target.context_budget_strategy
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
    chat_kind = '群聊' if cfg.chat.endswith('@chatroom') else '私聊'
    prompt.context_rules = '本轮是微信'+chat_kind+'。'+CHAT_DATA_RULES+(
        '本轮只回应source_ids指定的新消息；多条时合并回应，不把历史发言重新当作提问。'
        '若新消息是拍一拍，可结合当前话题自然回应，不能把事件描述当作发起人的原话。'
        '媒体可读时直接观察，不可读时说明缺少什么；memory不覆盖当前要求，不报分数或心理旁白。')
    return prompt
