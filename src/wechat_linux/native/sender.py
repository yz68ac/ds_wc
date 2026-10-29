"""Submit an explicit recipient through the private service helper; no desktop access."""
from __future__ import annotations

from wechat_linux.native.protocol import NativeError, request, validate_send
from wechat_linux.errors import SendError
from wechat_linux.native.options import NativeOptions
from wechat_linux.models import DeliveryState, SendReceipt

SAFE_ERRORS = {'native_service_unavailable','native_helper_timeout','native_worker_failed',
    'native_invalid_request','native_chat_not_allowed','native_wrong_client','native_profile_mismatch',
    'native_agent_error','native_no_unique_context','native_wrong_thread','native_context_changed',
    'native_construct_failed','native_schedule_failed','native_callback_timeout','native_worker_busy',
    'native_request_conflict','native_peer_rejected','native_body_mismatch','native_send_failed',
    'native_services_unavailable','native_cleanup_failed', 'native_image_unavailable',
    'native_image_changed', 'native_image_invalid', 'native_image_source_mismatch'}


def _submit(options, value):
    # Once IPC may have reached the helper, transport failure is uncertain.
    # Never switch to desktop or retry an uncertain submission.
    try:
        result=request(options.helper_socket,value,timeout=options.timeout_seconds,peer_uid=0)
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
    if value['op'] == 'send_image':
        if (metadata.get('message_type') != 3 or metadata.get('image_sha256') != value['image_sha256']
                or metadata.get('image_bytes') != value['image_bytes'] or metadata.get('image_mime') != value['image_mime']):
            raise SendError('native image result uncertain',code='native_worker_failed',metadata={'submitted':None})
        safe.update(message_type=3, image_sha256=value['image_sha256'], image_bytes=value['image_bytes'],
                    image_mime=value['image_mime'])
    return safe


def _readiness(options):
    result=request(options.helper_socket,dict(op='health'),timeout=min(5,options.timeout_seconds),peer_uid=0)
    if not result.get('ok') or result.get('result',{}).get('client_ready') is not True:
        raise ValueError('native_helper_not_ready')
    supported=result.get('result',{}).get('features',[])
    return dict(backend='native_service',ready=True,features=[name for name in ('send_text','send_image') if name in supported])


class NativeSender:
    """Unprivileged client of the existing isolated helper; a return is only submission."""
    def __init__(self,helper_socket, *, max_text_bytes=4096, timeout_seconds=22):
        self.options=NativeOptions(helper_socket,max_text_bytes,timeout_seconds)

    def submit_text(self,chat_id,text,*,request_id):
        value=validate_send(dict(op='send_text',chat_id=chat_id,text=text,request_id=request_id), self.options.max_text_bytes)
        metadata=_submit(self.options,value)
        return SendReceipt(request_id,chat_id,DeliveryState.SUBMITTED,metadata=metadata)

    def submit_image(self,chat_id,image_path,*,request_id):
        """Submit a local PNG/JPEG. Keep the file unchanged until delivery is confirmed."""
        from wechat_linux.native.image_send import image_request
        value=image_request(chat_id,image_path,request_id)
        metadata=_submit(self.options,value)
        return SendReceipt(request_id,chat_id,DeliveryState.SUBMITTED,metadata=metadata)

    def health(self):return _readiness(self.options)
