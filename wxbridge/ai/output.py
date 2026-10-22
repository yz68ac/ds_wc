"""群聊输出约束与纯文本兜底，不截断回复正文或另发模型请求。"""
from __future__ import annotations

import copy
import re

from wxbridge.ai.emoticons import is_token, semantics_prompt, token_pattern


REPLY_RULES = (
    "\n\n【群聊输出要求】沿用原有人格、语气和称呼偏好，联网回答也一样。"
    "先理解对方是在求解、分享、吐槽、开玩笑还是接上一句，再自然回应，不输出这些分类。"
    "常规闲聊和日常问答以短句为主，通常一句，几个字到二三十字都可以，尽量不超过30字；不凑固定字数。"
    "简单确认可以只回短短几个字，没有不可省略的新信息就停，不把每句话写成精心设计的台词。"
    "不为展示人设自动补调侃、建议、反问、叮嘱或客套，不另开吃饭休息等无关话题。"
    "参考上下文中BOT已发出的原话衔接话题，少重复刚讲过的结论、口头禅和句尾；明确要求复述或仍未弄清时可以解释。"
    "对方纠正了就直接修正，已回答的信息不再问；亲近程度和玩笑分寸看具体互动与明确偏好，不只看关系等级。"
    "确需补充时可分两条各自完整的短消息，用一个空行分隔；不要每次都拆两条。"
    "专业问答、故障分析、推理、步骤和代码按实际信息需要展开，可以超过30字，无须用户额外要求详细。"
    "先给结论，再给必要依据或操作，保持精炼，不铺背景、不写报告式模板、总结标题或固定收尾。"
    "不要硬删关键条件、必要事实、风险或不确定性；连续步骤、代码保持连贯，真实网址不按字数截断。"
    "不要直说对群友的考察、打分、隐藏动机或心理旁白；性格靠语气自然体现，不做自我解释。"
    "只发给群友看的正文，不加‘回复：’‘助手：’等前缀。"
    "务必只输出纯文本，不使用Markdown：不用标题标记、加粗、斜体、反引号、代码围栏、引用块、表格或项目符号。"
    "需要给链接时直接写网址，不用Markdown链接；需要代码时写普通文本。"
    "自然分句或换行即可，不把整条回复挤成长段，也不机械套用‘首先、其次、最后’。"
)


def reply_rules(cfg=None):
    settings = (cfg or {}).get('reply_style', {})
    rules = REPLY_RULES
    if settings.get('wechat_emoticons_enabled', False):
        rules += ('\n【本轮微信表情已开启】闲聊按语境偶尔用下方清单里的中文方括号表情，通常不用，每条最多一个。'
                  '适合接梗或简单回应时可以只回一个；需要事实、操作或安慰时不要只用表情敷衍。'
                  '不机械追加、不连续刷屏、不自造名称，不把[微笑]用于示好、感谢、安慰或友好收尾。'
                  '微信表情标记属于正文，不是Markdown。用户明确要求解释、引用或列出表情时可原样给出多个。')
    else:
        rules += ('\n【本轮自动微信表情已关闭】不要为装饰主动添加微信表情或单回微信表情。'
                  '群友原文、引用、代码、用户明确要求的表情解释或原样输出仍可保留，不机械删除方括号。')
    if settings.get('omit_trailing_period', True):
        rules += ('\n【日常标点】普通短句末尾不加中文句号“。”，表情前也不加句号。'
                  '问号、感叹号、逗号和省略号按语气保留；代码、命令、链接、小数、引用原文及专业长答保持准确。')
    return rules+semantics_prompt()


def response_prompt(messages, cfg=None):
    """不修改原提示词、图片或工具调用链；即使搜索关闭也应用输出偏好。"""
    conversation = copy.deepcopy(messages)
    if conversation and conversation[0].get("role") == "system":
        conversation[0]["content"] += reply_rules(cfg)
    else:
        conversation.insert(0, dict(role="system", content=reply_rules(cfg).strip()))
    return conversation


def plain_text(text):
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
            rendered.append(line)
            continue
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
        def keep_url(match):
            literals.append(match[0])
            return "\x00"+str(len(literals)-1)+"\x00"
        line = re.sub(r'https?://[^\s<>\x00]+', keep_url, line)
        for delimiter in ("**", "__", "~~", "*", "_"):
            escaped = re.escape(delimiter)
            line = re.sub(r'(?<![A-Za-z0-9_*~])'+escaped+r'(?=\S)(.+?\S|\S)'+escaped+r'(?![A-Za-z0-9_*~])', r'\1', line)
        line = re.sub(r'\x00(\d+)\x00', lambda m: literals[int(m[1])] if int(m[1]) < len(literals) else m[0], line)
        rendered.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(rendered)).strip()


def format_reply(text, cfg=None):
    """同一出口：保留原文/技术结构，只为保守识别的普通短句去中文尾句号。"""
    original = text
    result = plain_text(text)
    if not (cfg or {}).get('reply_style', {}).get('omit_trailing_period', True):
        return result
    if not result or len(result) > 30 or '\n' in result:
        return result
    # 引用和代码在去 Markdown 前识别，避免去掉原样字符串的内容。
    if (re.search(r"[`'\"“”‘’=<>\\{}]|https?://|\d\.\d", original)
            or re.match(r'^\s*(?:>|#{1,6}\s|[-*+]\s)', original)):
        return result
    prose = re.sub(token_pattern(), '', result)
    if re.fullmatch(token_pattern()+r'。+', result):
        return result.rstrip('。')
    if not re.search(r'[\u3400-\u9fff]', prose) or '[' in prose or ']' in prose:
        return result
    if re.match(r'\s*(?:sudo|echo|printf|python[\d.]*|git|curl|systemctl|ss|lsof|export)\s', prose):
        return result
    # 最多两次，兼容“知道啦。[偷笑]。”；不删句内标点或 ASCII 的句点。
    for _ in range(2):
        result = re.sub(r'。+\s*('+token_pattern()+r')?$', lambda m: m[1] or '', result)
    return result
