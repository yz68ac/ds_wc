"""UI preparation only; a private helper constructs and submits native text objects."""
from __future__ import annotations

from wxbridge.native.protocol import NativeError, request, settings, validate_send
from wxbridge.wechat.ui import Sender, UIError

SAFE_ERRORS = {'native_service_unavailable','native_helper_timeout','native_worker_failed',
    'native_invalid_request','native_chat_not_allowed','native_wrong_client','native_profile_mismatch',
    'native_agent_error','native_no_unique_context','native_wrong_thread','native_context_changed',
    'native_construct_failed','native_schedule_failed','native_callback_timeout','native_worker_busy',
    'native_request_conflict','native_peer_rejected','native_body_mismatch','native_send_failed'}


def submit(cfg, title, text, chat_id, request_id):
    options=settings(cfg)
    value=validate_send(dict(op='send_text',chat_id=chat_id,text=text,request_id=request_id))
    if not options['enabled'] or chat_id not in options['allowed_chat_ids']:
        raise UIError('native disabled',code='native_chat_not_allowed',metadata={'submitted':False})
    try:
        sender=Sender(cfg)
        if sender.dry_run:
            raise UIError('native dry run',code='native_chat_not_allowed')
        if not sender.a11y.available:
            raise UIError('native requires verified composer',code='native_no_unique_context')
        sender.a11y.select_chat(title)
        if sender.a11y.chat_is_selected(title) is not True:
            raise UIError('native wrong chat',code='native_no_unique_context')
        node,_=sender.a11y.composer()
        if sender.a11y.read_text(node):
            raise UIError('native protects draft',code='draft_conflict')
    except Exception as exc:
        code=getattr(exc,'code','native_no_unique_context')
        if code not in SAFE_ERRORS|{'draft_conflict','wrong_chat'}:code='native_no_unique_context'
        raise UIError('native preparation rejected',code=code,metadata={'submitted':False}) from None
    # Once IPC may have reached the helper, transport failure is uncertain.
    # Never switch to desktop or retry an uncertain submission.
    try:
        result=request(options['helper_socket'],value,timeout=22)
    except NativeError as exc:
        raise UIError('native helper unavailable or uncertain',code='native_service_unavailable',
                      metadata={'submitted':exc.submitted}) from None
    if set(result) - {'ok','ui'} or not isinstance(result.get('ui'),dict):
        raise UIError('invalid helper response',code='native_worker_failed',metadata={'submitted':None})
    metadata=result['ui']
    operation=metadata.get('operation') or {}
    submitted=operation.get('submitted')
    if submitted is not None and type(submitted) is not bool:submitted=None
    safe=dict(backend='native',operation={'submitted':submitted})
    for key in ('chars','bytes','scanned_mib','main_thread'):
        if type(metadata.get(key)) is int:safe[key]=metadata[key]
    if result.get('ok') is not True:
        code=metadata.get('error')
        if code not in SAFE_ERRORS:code='native_worker_failed'
        raise UIError('native submission failed',code=code,metadata=safe['operation'])
    if submitted is not True:
        raise UIError('native result uncertain',code='native_worker_failed',metadata={'submitted':None})
    return safe
