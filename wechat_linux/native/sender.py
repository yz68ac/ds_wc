"""Submit an explicit recipient through the private service helper; no desktop access."""
from __future__ import annotations

from wechat_linux.native.protocol import NativeError, request, settings, validate_send
from wechat_linux.errors import SendError

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
        raise SendError('native disabled',code='native_chat_not_allowed',metadata={'submitted':False})
    if cfg['ui'].get('dry_run'):
        raise SendError('native dry run',code='native_chat_not_allowed',metadata={'submitted':False})
    # Once IPC may have reached the helper, transport failure is uncertain.
    # Never switch to desktop or retry an uncertain submission.
    try:
        result=request(options['helper_socket'],value,timeout=22)
    except NativeError as exc:
        raise SendError('native helper unavailable or uncertain',code='native_service_unavailable',
                      metadata={'submitted':exc.submitted}) from None
    if set(result) - {'ok','ui'} or not isinstance(result.get('ui'),dict):
        raise SendError('invalid helper response',code='native_worker_failed',metadata={'submitted':None})
    metadata=result['ui']
    operation=metadata.get('operation') or {}
    submitted=operation.get('submitted')
    if submitted is not None and type(submitted) is not bool:submitted=None
    safe=dict(backend='native_service',operation={'submitted':submitted})
    for key in ('chars','bytes','main_thread'):
        if type(metadata.get(key)) is int:safe[key]=metadata[key]
    if metadata.get('recipient_verified') is True:safe['recipient_verified']=True
    timing=metadata.get('timings_ms') or {}
    safe['timings_ms']={k:timing[k] for k in ('verify','attach','submit')
                        if type(timing.get(k)) in (int,float) and 0<=timing[k]<=60000}
    if result.get('ok') is not True:
        code=metadata.get('error')
        if code not in SAFE_ERRORS:code='native_worker_failed'
        raise SendError('native submission failed',code=code,metadata=safe['operation'])
    if submitted is not True or metadata.get('backend')!='native_service' or metadata.get('recipient_verified') is not True:
        raise SendError('native result uncertain',code='native_worker_failed',metadata={'submitted':None})
    return safe


def readiness(cfg):
    options=settings(cfg)
    if not options['enabled']:
        raise ValueError('native_api_disabled')
    result=request(options['helper_socket'],dict(op='health'),timeout=5)
    if not result.get('ok') or result.get('result',{}).get('client_ready') is not True:
        raise ValueError('native_helper_not_ready')
    return dict(backend='native_service',ready=True)


class NativeSender:
    """Unprivileged client of the existing isolated helper; a return is only submission."""
    def __init__(self,helper_socket,allowed_chats):
        self.cfg=dict(ui=dict(backend='native_service',dry_run=False),native_api=dict(
            enabled=True,socket='/run/wechat-linux/client.sock',helper_socket=str(helper_socket),
            allowed_chat_ids=list(allowed_chats),max_text_bytes=4096,max_pending=32,max_requests=4096))
        settings(self.cfg)

    def __call__(self,chat_id,text,request_id):
        return submit(self.cfg,'',text,chat_id,request_id)

    def submit_text(self,chat_id,text,*,request_id):
        from wechat_linux.models import DeliveryState,SendReceipt
        self(chat_id,text,request_id)
        return SendReceipt(request_id,chat_id,DeliveryState.SUBMITTED)

    def health(self):return readiness(self.cfg)
