"""Reusable Linux WeChat adapter. No model configuration or implicit I/O on import."""
from wechat_linux.models import DeliveryState,Image,Message,MessageBatch,SendReceipt
from wechat_linux.errors import WeChatError,NativeError,OwnerBusyError,SendError,StoreError,MediaError,DependencyError

__version__='0.1.0'
__all__=['WeChatSession','SessionConfig','NativeClient','NativeSender','DeliveryState','Image','Message',
         'MessageBatch','SendReceipt','WeChatError','NativeError','OwnerBusyError','SendError','StoreError','MediaError',
         'DependencyError','capabilities']


def __getattr__(name):
    if name in ('WeChatSession','SessionConfig'):
        from wechat_linux import session
        return getattr(session,name)
    if name=='NativeClient':
        from wechat_linux.native.client import NativeClient
        return NativeClient
    if name=='NativeSender':
        from wechat_linux.native.sender import NativeSender
        return NativeSender
    if name=='capabilities':
        from wechat_linux.profiles import capabilities
        return capabilities
    raise AttributeError(name)
