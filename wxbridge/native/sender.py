"""Submit an explicit recipient through the private service helper; no desktop access."""
from __future__ import annotations

from wxbridge.native.protocol import NativeError, request, settings, validate_send
from wxbridge.wechat.ui import UIError

SAFE_ERRORS = {'native_service_unavailable','native_helper_timeout','native_worker_failed',
    'native_invalid_request','native_chat_not_allowed','native_wrong_client','native_profile_mismatch',
    'native_agent_error','native_no_unique_context','native_wrong_thread','native_context_changed',
    'native_construct_failed','native_schedule_failed','native_callback_timeout','native_worker_busy',
    'native_request_conflict','native_peer_rejected','native_body_mismatch','native_send_failed',
    'native_services_unavailable','native_cleanup_failed'}


def submit(cfg, title, text, chat_id, request_id):
    options=settings(cfg)
    value=validate_send(dict(op='send_text',chat_id=chat_id,text=text,request_id=request_id))
    if not options['enabled'] or chat_id not in options['allowed_chat_ids']:
        raise UIError('native disabled',code='native_chat_not_allowed',metadata={'submitted':False})
    if cfg['ui'].get('dry_run'):
        raise UIError('native dry run',code='native_chat_not_allowed',metadata={'submitted':False})
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
    safe=dict(backend='native_service',operation={'submitted':submitted})
    for key in ('chars','bytes','main_thread'):
        if type(metadata.get(key)) is int:safe[key]=metadata[key]
    if metadata.get('recipient_verified') is True:safe['recipient_verified']=True
    if result.get('ok') is not True:
        code=metadata.get('error')
        if code not in SAFE_ERRORS:code='native_worker_failed'
        raise UIError('native submission failed',code=code,metadata=safe['operation'])
    if submitted is not True or metadata.get('backend')!='native_service' or metadata.get('recipient_verified') is not True:
        raise UIError('native result uncertain',code='native_worker_failed',metadata={'submitted':None})
    return safe
