"""Compatibility import; implementation lives in wechat_linux.media.wxgf."""
import importlib as _importlib
import sys as _sys
if __name__ == "__main__":
    import runpy
    _target = _importlib.import_module("wechat_linux.media.wxgf")
    runpy.run_path(_target.__file__, run_name="__main__")
else:
    _sys.modules[__name__] = _importlib.import_module("wechat_linux.media.wxgf")
