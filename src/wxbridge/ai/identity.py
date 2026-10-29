"""One per-request identity map for messages and nested quotes; no IO."""
from wxbridge.wechat.messages import display_reference


def member_labels(context, triggers, account, *, initial=None):
    labels = dict(initial or {})
    rows = ([triggers[-1]] if triggers else []) + list(triggers) + list(context)
    def add(uid):
        if uid and uid != account and uid not in labels:
            number = 1
            while 'U'+str(number) in labels.values():
                number += 1
            labels[uid] = 'U'+str(number)
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
    return 'BOT' if account and uid == account else labels.get(uid, 'UNKNOWN')


def member_name(message, labels, account):
    if member_tag(message, labels, account) == 'BOT':
        return 'BOT（你）'
    name = message.get('sender_name') or message.get('sender') or '群友'
    label = labels.get(message.get('sender'))
    return f'{label}（{name}）' if label else f'群友（{name}）' if account else name


def reference_data(reference, labels, account, *, depth=0):
    value = prompt_reference(reference)
    value.pop('reference', None)
    value.update(member=member_tag(reference, labels, account),
                 sender_name=member_name(reference, labels, account))
    if isinstance(reference.get('reference'), dict) and depth < 7:
        value['reference'] = reference_data(reference['reference'], labels, account, depth=depth+1)
    return value


def prompt_reference(reference):
    """给模型的显示字段；不传内部账号、server_id 或原始 XML。"""
    reference = display_reference(reference)
    fields = ("kind", "type", "sender_name", "time", "available", "text", "reason",
              "image_label", "image_quality", "image_width", "image_height")
    result = {key: reference[key] for key in fields if key in reference}
    if isinstance(reference.get("reference"), dict):
        result["reference"] = prompt_reference(reference["reference"])
    return result
