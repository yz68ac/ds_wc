"""Detached, source-bound picture candidates; no database or filesystem I/O."""
from copy import deepcopy
import re

from wxbridge.ai.context.selection import message_key


def image_source(message):
    """Only immutable source fields, never local paths or model descriptions."""
    return tuple(message.get(k) for k in ('local_id', 'sender', 'time', 'server_id', 'image_md5'))


def image_candidates(cfg, context, triggers, prompt, *, chat_id=None):
    candidates, groups = [], {}

    def add(source, target, *, history=False):
        # Same bytes from the same sender may share a block, but never a sender
        # identity. A digest-less quote must not deduplicate unrelated pictures.
        scope = source.get('chat_id') or chat_id or cfg.chat
        keys = [(scope, kind, source.get('sender'), source[field]) for kind, field, pattern in
                (('md5', 'image_md5', r'[0-9a-f]{32}'), ('server', 'server_id', r'[1-9][0-9]{0,19}'))
                if isinstance(source.get(field), str) and re.fullmatch(pattern, source[field])]
        for key in keys:
            previous = groups.get(key)
            if previous is None:continue
            old_digest, new_digest = previous['source'].get('image_md5'), source.get('image_md5')
            if old_digest and new_digest and old_digest != new_digest:continue
            old_id, new_id = previous['source'].get('server_id'), source.get('server_id')
            if (old_id and new_id and old_id != new_id
                    and not (previous['source'].get('local_id') and source.get('local_id'))):continue
            previous['targets'].append(target)
            if history:previous['source'] = deepcopy(source)
            for alias in keys:groups[alias] = previous
            return
        if history and len(candidates) >= cfg.vision.max_images:return
        value = dict(source=deepcopy(source), targets=[target])
        candidates.append(value)
        for key in keys:groups[key] = value

    for index, message in enumerate(triggers):
        reference, depth = message.get('reference'), 0
        while isinstance(reference, dict) and depth < 8:
            if reference.get('kind') == 'image':
                add(reference, dict(trigger_index=index, depth=depth))
            reference, depth = reference.get('reference'), depth+1
    if cfg.vision.enabled and cfg.vision.context_images_enabled:
        layout = getattr(prompt, 'layout', None)
        retained = {m['id'] for m in layout.payload['messages']} if layout else set()
        for message in sorted(context, key=message_key, reverse=True):
            if message.get('type') == 3 and message['local_id'] in retained:
                add(message, dict(message_id=message['local_id'], source=image_source(message)), history=True)
    return candidates
