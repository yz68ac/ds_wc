"""Presentation-only citation cleanup; retain facts, functional URLs and wire state."""
import re
from urllib.parse import urlsplit

LITERAL = re.compile(r"""```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|"(?:\\.|[^"\\\n])*"|(?<![A-Za-z0-9])'(?:\\.|[^'\\\n])*'(?![A-Za-z0-9])|“[^”\n]*”|‘[^’\n]*’""")
LINK = re.compile(r'\[([^\]\n]*)\]\((<?(?:[^\s()]|\([^\n()]*\))+>?)(?:\s+"[^"\n]*")?\)')


def without_citations(text, sources, annotations=()):
    urls = {r['url'] for r in list(sources)[:128] if isinstance(r, dict) and isinstance(r.get('url'), str)}
    if not urls:
        return text
    def drop_links(value, selected):
        return LINK.sub(lambda m: '' if m[2].strip('<>') in selected else m[0], value)
    protected = [(m.start(), m.end()) for m in LITERAL.finditer(text)]
    edits = []
    for row in list(annotations)[:128]:
        if not isinstance(row, dict) or row.get('url') not in urls:
            continue
        start, end = row.get('start_index'), row.get('end_index')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            continue
        if any(start < b and end > a for a, b in protected):
            continue
        fragment = text[start:end]
        replacement = drop_links(fragment, {row['url']})
        if re.fullmatch(r'\s*(?:\[\d+(?:[, -]\d+)*\]|\ue200cite\ue202[^\ue201]*\ue201)\s*', fragment):
            replacement = ''
        if replacement == fragment:
            continue  # A citation over a factual sentence never means deleting that sentence.
        if not replacement.strip() and start and end < len(text) and (text[start-1], text[end]) in (('(', ')'), ('（', '）')):
            start -= 1
            end += 1
        edits.append((start, end, replacement))
    boundary = len(text)
    for start, end, replacement in sorted(edits, reverse=True):
        if end <= boundary:
            text = text[:start]+replacement+text[end:]
            boundary = start
    def prose(value):
        lines, reference_block, header = [], False, ''
        for line in value.splitlines(keepends=True):
            if re.fullmatch(r'\s*(?:#{1,6}\s*)?(?:参考(?:资料|来源|链接)?|来源|sources?|references?)\s*[:：]?\s*', line, re.I):
                reference_block, header = True, line
                continue
            if reference_block and 'http' in line.lower():
                header = ''
                continue
            if reference_block:
                if header:
                    lines.append(header)
                reference_block, header = False, ''
            lines.append(line)
        if header:
            lines.append(header)
        value = ''.join(lines)
        value = re.sub(r'(?im)^[ \t]*(?:[-*]\s*)?(?:参考(?:资料|来源|链接)?|来源|sources?|references?)\s*[:：][^\n]*https?://[^\n]*', '', value)
        def group(match):
            prefix = value[max(0, match.start()-16):match.start()]
            if re.search(r'(?:官网|下载(?:地址|链接)?|地址|链接|url|link)\s*[:：]?\s*$', prefix, re.I):
                return match[0]
            content = drop_links(match[1], urls)
            for url in sorted(urls, key=len, reverse=True):
                content = re.sub(re.escape(url)+r'(?=$|[\s,;，；、])', '', content)
            if content == match[1]:
                return match[0]
            residue = re.sub(r'(?i)sources?|references?|来源|参考(?:资料|链接)?', '', content)
            if re.fullmatch(r'[\s,:;，；、.]*', residue):
                return ''
            return match[0][0]+content+match[0][-1]
        value = re.sub(r'(?<!\])[（(]((?:[^()（）\n]|\([^()\n]*\))*)[)）]', group, value)
        def source_link(match):
            url, label = match[2].strip('<>'), match[1].strip()
            if url not in urls:
                return match[0]
            host = urlsplit(url).hostname or ''
            is_reference = re.fullmatch(r'(?:来源|参考(?:资料|链接)?|sources?|references?)(?:\s*\d+)?|\d+', label, re.I)
            return '' if is_reference or label.lower() == host.lower() else match[0]
        value = LINK.sub(source_link, value)
        value = re.sub(r'\ue200cite\ue202[^\ue201]*\ue201', '', value)
        return value
    parts, cursor = [], 0
    for match in LITERAL.finditer(text):
        parts.extend((prose(text[cursor:match.start()]), match[0]))
        cursor = match.end()
    parts.append(prose(text[cursor:]))
    return ''.join(parts).strip()
