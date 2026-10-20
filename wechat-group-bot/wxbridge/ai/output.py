"""群聊输出约束与纯文本兜底，不截断回复正文或另发模型请求。"""
from __future__ import annotations

import copy
import re


REPLY_RULES = (
    "\n\n【群聊输出要求】沿用原有人格、语气和称呼偏好，联网回答也一样。"
    "默认用两到四句短话回答，正文尽量控制在80至160字，通常不超过200字，最多两小段；简单问题一句就够。"
    "先直接说判断，再挑最有用的理由，不铺背景、不写报告式模板、总结标题或固定收尾。"
    "只有用户明确要求详细解释、完整步骤或长内容时才展开；不要硬删必要事实、风险或不确定性。"
    "务必只输出纯文本，不使用Markdown：不用标题标记、加粗、斜体、反引号、代码围栏、引用块、表格或项目符号。"
    "需要给链接时直接写网址，不用Markdown链接；需要代码时写普通文本。"
    "自然分句或换行即可，不把整条回复挤成长段，也不机械套用‘首先、其次、最后’。"
)


def response_prompt(messages):
    """不修改原提示词、图片或工具调用链；即使搜索关闭也应用输出偏好。"""
    conversation = copy.deepcopy(messages)
    if conversation and conversation[0].get("role") == "system":
        conversation[0]["content"] += REPLY_RULES
    else:
        conversation.insert(0, dict(role="system", content=REPLY_RULES.strip()))
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
        line = re.sub(r'!?\[([^\]\n]*)\]\((<?(?:[^\s()]|\([^\n()]*\))+>?)(?:\s+"[^"\n]*")?\)',
                      lambda m: (m[1]+" "+m[2].strip("<>")) if m[1] != m[2].strip("<>") else m[1], line)
        line = re.sub(r'\[([^\]\n]+)\]\[([^\]\n]*)\]',
                      lambda m: m[1]+(" "+definitions[(m[2] or m[1]).casefold()] if (m[2] or m[1]).casefold() in definitions else ""), line)
        line = re.sub(r'<(https?://[^<>\s]+)>', r'\1', line)
        literals = []
        def keep_code(match):
            literals.append(match[2])
            return "\x00"+str(len(literals)-1)+"\x00"
        line = re.sub(r'(`+)(.+?)\1', keep_code, line)
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
