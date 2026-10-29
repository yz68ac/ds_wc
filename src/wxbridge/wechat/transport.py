"""Shared selection and validation; this module never loads GUI dependencies."""
BACKENDS = ('auto', 'atspi', 'xdotool', 'native_service')


def backend(cfg):
    value = cfg.get('ui', {}).get('backend', 'auto')
    if value not in BACKENDS:
        raise ValueError('ui.backend must be auto/atspi/xdotool/native_service')
    return value


def native(cfg, *, external=False):
    return external or backend(cfg) == 'native_service'
