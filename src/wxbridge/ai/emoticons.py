"""Compatibility alias for model-independent expression reference data."""
import sys
from wxbridge import emoticons as _module
sys.modules[__name__] = _module
