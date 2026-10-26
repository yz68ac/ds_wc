"""Compatibility import; implementation lives in wechat_linux.sending.transport."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("wechat_linux.sending.transport")
