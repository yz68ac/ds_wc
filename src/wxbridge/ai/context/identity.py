"""Use WeChat IDs for identity and group aliases only for presentation; no IO."""
from wxbridge.wechat.messages import display_reference


def member_labels(context, triggers, account, *, initial=None):
    # Keep the mapping contract used by memory rendering without allocating
    # request-local U numbers. Old caller-supplied label values are ignored.
    labels = {uid: uid for uid in (initial or {}) if isinstance(uid, str) and uid and uid != account}
    rows = list(triggers) + list(context)
    def add(uid):
        if isinstance(uid, str) and uid and uid != account:
            labels[uid] = uid
    for message in rows:
        add(message.get('sender'))
    for message in rows:
        ref = message.get('reference')
        for _ in range(8):
            if not isinstance(ref, dict):
                break
            add(ref.get('sender'))
            ref = ref.get('reference')
    return labels


def member_tag(message, labels, account):
    uid = message.get('sender')
    return uid if isinstance(uid, str) else ''


def member_alias(message, aliases=None):
    """An explicit profile alias, including an empty one, wins over contacts."""
    uid = message.get('sender')
    if aliases is not None:
        value = aliases.get(uid, '')
        value = value.get('alias', '') if isinstance(value, dict) else value
    else:
        value = message.get('alias', message.get('sender_name', ''))
    return value if isinstance(value, str) else ''


def member_name(message, labels, account, *, aliases=None):
    uid = member_tag(message, labels, account)
    alias = member_alias(message, aliases)
    if account and uid == account:
        return f'{uid}（你）'
    if not uid:
        return '未知发言者'
    return f'{uid}（{alias}）' if alias else uid


def reference_data(reference, labels, account, *, depth=0, aliases=None):
    value = prompt_reference(reference)
    value.pop('reference', None)
    value.pop('sender_name', None)
    value.update(member=member_tag(reference, labels, account),
                 alias=member_alias(reference, aliases),
                 is_bot=bool(account and reference.get('sender') == account))
    if isinstance(reference.get('reference'), dict) and depth < 7:
        value['reference'] = reference_data(reference['reference'], labels, account, depth=depth+1, aliases=aliases)
    return value


def prompt_reference(reference):
    """给模型的引用内容；身份由 reference_data 绑定，不传原始 XML。"""
    reference = display_reference(reference)
    fields = ("kind", "type", "sender_name", "time", "available", "text", "reason",
              "image_label", "image_quality", "image_width", "image_height")
    result = {key: reference[key] for key in fields if key in reference}
    if isinstance(reference.get("reference"), dict):
        result["reference"] = prompt_reference(reference["reference"])
    return result
