"""Freeze per-request member labels only with current, validated source bindings."""
import copy
import re

MEMBER_LABEL = re.compile(r'(?<![A-Za-z0-9_])U[1-9][0-9]*(?![A-Za-z0-9_])')


def ambiguous_fields(record, fields):
    tokens=set().union(*(set(MEMBER_LABEL.findall(record.get(k, ''))) for k in fields))
    return bool(tokens-set(record.get('literal_labels', [])))


def bind_text(text, owner, mapping, members, source_texts):
    literals=set().union(*(set(MEMBER_LABEL.findall(s)) for s in source_texts))
    used=set()
    def replace(match):
        label=match.group()
        if label in literals:
            used.add(label)
            return label
        uid=mapping.get(label)
        if uid is None:
            raise ValueError('memory_unbound_label')
        return '本人' if uid==owner else members[uid]['nickname']
    return MEMBER_LABEL.sub(replace,text),sorted(used)


def safe_legacy_metadata(value):
    """Hide ambiguous old prose from AI backgrounds without rewriting score history."""
    if isinstance(value,dict):
        return {k:safe_legacy_metadata(v) for k,v in value.items()}
    if isinstance(value,list):
        return [safe_legacy_metadata(v) for v in value]
    if isinstance(value,str):
        return MEMBER_LABEL.sub('（旧记录中的成员指代不明确）',value)
    return copy.deepcopy(value)
