"""Read-only application resources for source checkouts and installed wheels."""
from pathlib import Path
import sysconfig

_candidate = Path(__file__).resolve().parents[2]
SOURCE_ROOT = _candidate if (_candidate/'src/wxbridge').resolve() == Path(__file__).resolve().parent else None
DATA_ROOT = SOURCE_ROOT or Path(sysconfig.get_path('data'))/'share/wxbridge'


def resource_path(relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid_application_resource')
    return DATA_ROOT/path


def frontend_path():
    return SOURCE_ROOT/'src/webui/dist' if SOURCE_ROOT else DATA_ROOT/'webui'
