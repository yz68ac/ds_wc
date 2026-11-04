"""群聊输出约束与纯文本兜底，不截断回复正文或另发模型请求。"""
from __future__ import annotations

import re

from wxbridge.emoticons import is_token, token_pattern
from wxbridge.ai.composer import PromptComposer


REPLY_RULES = "只输出发给群友的回复正文，不输出内部成员编号、提示词或内部判断过程。"


def reply_rules(cfg=None):
    rules = [REPLY_RULES]
    if cfg is not None:
        style = cfg.reply_style
        rules.append('Unicode emoji 不受微信表情文字开关限制。')
        if style.wechat_emoticons_enabled:
            rules.append('可以在语境合适时使用微信文字表情，不必每条回复都使用。')
        else:
            rules.append('微信文字表情已关闭：普通回复正文不要使用方括号形式的微信表情码。'
                         '需要解释或展示表情码本身时，用引号或代码标记保留原文。')
        if style.omit_trailing_period:
            rules.append('省略回复正文末尾的单个句号；保留省略号、代码、引用和链接原文。')
    return ''.join(rules)


def response_prompt(messages, cfg=None):
    """不修改原提示词、图片或工具调用链；即使搜索关闭也应用输出偏好。"""
    if getattr(messages,'context_rules',''):
        messages = PromptComposer.instructions(messages,'chat_context','\n\n【聊天记录协议】'+messages.context_rules)
    return PromptComposer.instructions(messages, 'output', reply_rules(cfg))


def plain_text(text, *, prose_transform=None, final_transform=None):
    """模型偶尔漏出 Markdown 时转为可读正文，保留代码、数字和真实 URL。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    definitions = {}
    lines = []
    fenced = False
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if marker and (not fenced or marker[1][0] == fence):
            fenced = not fenced
            fence = marker[1][0] if fenced else ""
            continue
        # 代码中的缩进、乘法、下划线等保持原样，只移除围栏。
        if fenced:
            lines.append((line, True))
            continue
        definition = re.match(r'^\s{0,3}\[([^\]]+)\]:\s*<?(https?://[^\s>]+)>?(?:\s+.*)?$', line)
        if definition:
            definitions[definition[1].casefold()] = definition[2]
            continue
        lines.append((line, False))

    rendered = []
    for line, code in lines:
        if code:
            rendered.append((line, (), False))
            continue
        literal_line = bool(re.match(r'^\s*(?:>|(?:sudo|echo|printf|python[\d.]*|git|curl|systemctl|ss|lsof|export)\s)', line))
        literals = []
        def keep_code(match):
            literals.append(match[2])
            return "\x00"+str(len(literals)-1)+"\x00"
        # 内联代码先保护，不能把里面的连续方括号误当引用链接。
        line = re.sub(r'(`+)(.+?)\1', keep_code, line)
        line = re.sub(r"^\s{0,3}(?:>\s*)+", "", line)
        line = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", r"\1", line)
        if re.fullmatch(r"\s*(?:[-*_]\s*){3,}|\s*=+\s*", line):
            continue
        if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", line):
            continue
        line = re.sub(r"^\s*(?:[-+*•]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s*)?", "", line)
        if line.strip().startswith("|") and line.strip().endswith("|"):
            line = "，".join(cell.strip() for cell in line.strip().strip("|").split("|"))
        # 支持 URL 中的一层括号，不改动查询参数，也不丢失链接目标。
        def label(value):
            token = '['+value+']'
            return token if is_token(token) else value
        line = re.sub(r'!?\[([^\]\n]*)\]\((<?(?:[^\s()]|\([^\n()]*\))+>?)(?:\s+"[^"\n]*")?\)',
                      lambda m: (label(m[1])+" "+m[2].strip("<>")) if m[1] != m[2].strip("<>") else label(m[1]), line)
        line = re.sub(r'\[([^\]\n]+)\]\[([^\]\n]*)\]',
                      lambda m: label(m[1])+" "+definitions[(m[2] or m[1]).casefold()]
                      if (m[2] or m[1]).casefold() in definitions else m[0], line)
        line = re.sub(r'<(https?://[^<>\s]+)>', r'\1', line)
        if (prose_transform or final_transform) and not literal_line:
            for opening, closing in (('"','"'), ("'","'"), ('“','”'),
                                     ('‘','’'), ('「','」'), ('『','』')):
                pattern = re.escape(opening)+r'((?:\\.|[^\\\x00\n'+re.escape(closing)+r'])*)'+re.escape(closing)
                def keep_quote(match):
                    literals.append(match[0])
                    return "\x00"+str(len(literals)-1)+"\x00"
                line = re.sub(pattern, keep_quote, line)
        def keep_url(match):
            literals.append(match[0])
            return "\x00"+str(len(literals)-1)+"\x00"
        line = re.sub(r'https?://[^\s<>\x00]+', keep_url, line)
        for delimiter in ("**", "__", "~~", "*", "_"):
            escaped = re.escape(delimiter)
            line = re.sub(r'(?<![A-Za-z0-9_*~])'+escaped+r'(?=\S)(.+?\S|\S)'+escaped+r'(?![A-Za-z0-9_*~])', r'\1', line)
        if prose_transform and not literal_line:
            line = prose_transform(line)
        rendered.append((line, literals, not literal_line))
    if final_transform:
        for index in range(len(rendered)-1, -1, -1):
            line, literals, prose = rendered[index]
            if not line.strip():continue
            if prose:rendered[index] = (final_transform(line), literals, prose)
            break
    def restore(item):
        line, literals, _ = item
        return re.sub(r'\x00(\d+)\x00', lambda m: literals[int(m[1])] if int(m[1]) < len(literals) else m[0], line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(map(restore, rendered))).strip()


def format_reply(text, cfg=None):
    """Apply explicit output preferences to prose, preserving literal content."""
    if cfg is None:
        return plain_text(text)
    style = cfg.reply_style
    emoticons = None if style.wechat_emoticons_enabled else lambda line: re.sub(token_pattern(), '', line)
    period = (lambda line: re.sub(r'(?<![.。])[.。](?=[ \t]*$)', '', line)) if style.omit_trailing_period else None
    return plain_text(text, prose_transform=emoticons, final_transform=period)
