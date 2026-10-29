"""Quarantine legacy temporary member labels; current identities are wxids."""
import copy
import re

MEMBER_LABEL = re.compile(r'(?<![A-Za-z0-9_])U[1-9][0-9]*(?![A-Za-z0-9_])')


def ambiguous_fields(record, fields):
    tokens=set().union(*(set(MEMBER_LABEL.findall(record.get(k, ''))) for k in fields))
    return bool(tokens-set(record.get('literal_labels', [])))


def bind_text(text, owner, mapping, members, source_texts):
    # Current mappings are wxid -> wxid. A generated U token therefore cannot
    # silently become a newly numbered member; only explicit old bindings or
    # literal U tokens in the validated source text can be retained.
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
        return '本人' if uid==owner else uid
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
