"""Reusable Linux WeChat adapter. No model configuration or implicit I/O on import."""
from wechat_linux.models import DeliveryState,Image,Message,MessageBatch,SendReceipt
from wechat_linux.errors import WeChatError,NativeError,OwnerBusyError,SendError,StoreError,MediaError,DependencyError,ImageDownloadError

__version__='0.2.0'
__all__=['WeChatSession','SessionConfig','NativeSender','DeliveryState','Image','Message',
         'MessageBatch','SendReceipt','WeChatError','NativeError','OwnerBusyError','SendError','StoreError','MediaError',
         'DependencyError','capabilities','NativeImageDownloader','ImageDownloadError','NativeOptions',
         'LoginController','LoginStatus','LoginQRCode','LoginError']


def __getattr__(name):
    if name in ('LoginController','LoginStatus','LoginQRCode','LoginError'):
        from wechat_linux.desktop import login
        return getattr(login,name)
    if name in ('WeChatSession','SessionConfig'):
        from wechat_linux import session
        return getattr(session,name)
    if name=='NativeSender':
        from wechat_linux.native.sender import NativeSender
        return NativeSender
    if name=='NativeOptions':
        from wechat_linux.native.options import NativeOptions
        return NativeOptions
    if name=='NativeImageDownloader':
        from wechat_linux.native.image_download import NativeImageDownloader
        return NativeImageDownloader
    if name=='capabilities':
        from wechat_linux.profiles import capabilities
        return capabilities
    raise AttributeError(name)
