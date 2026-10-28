"""Shared errors; importing the SDK never starts a session or privileged helper."""


class WeChatError(Exception):
    pass


class StoreError(WeChatError, RuntimeError):
    pass


class OwnerBusyError(WeChatError, ValueError):
    pass


class UIError(WeChatError, RuntimeError):
    def __init__(self, message, *, code='ui_error', metadata=None):
        super().__init__(message)
        self.code, self.metadata = code, metadata


class WrongChatError(UIError):
    pass


class SendError(UIError):
    @property
    def submitted(self):
        return (self.metadata or {}).get('submitted')


class NativeError(WeChatError, ValueError):
    def __init__(self, code, *, submitted=None):
        super().__init__(code)
        self.code, self.submitted = code, submitted


class DependencyError(WeChatError, RuntimeError):
    pass


class MediaError(WeChatError, ValueError):
    pass


class ImageDownloadError(MediaError):
    def __init__(self, code, *, requested=None):
        super().__init__(code)
        self.code, self.requested = code, requested
