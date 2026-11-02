"""Root-owned Web settings, independent of HTTP and command-line frameworks."""
import ipaddress
import os
from pathlib import Path
import stat
from dataclasses import dataclass, asdict

from .system import atomic_json, private_json

WEB_CONFIG = Path('/etc/wxbridge/web.json')
WEB_PASSWORD = Path('/etc/wxbridge/web-password')


def bind_host(value):
    if not isinstance(value, str):
        raise ValueError('web_host_invalid')
    if value == 'localhost':
        return '127.0.0.1'
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('web_host_invalid') from None
    private = address.version == 4 and any(address in ipaddress.ip_network(net)
        for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not address.is_loopback and not private:
        raise ValueError('web_host_not_local_or_private')
    return str(address)


@dataclass(frozen=True, slots=True)
class WebConfig:
    host: str = '127.0.0.1'
    port: int = 8730

    def __post_init__(self):
        object.__setattr__(self, 'host', bind_host(self.host))
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('web_port_invalid')


DEFAULT_WEB = WebConfig()


def web_document(value):
    return asdict(value) if value is not None else None


def validate_web(value):
    if not isinstance(value, dict) or set(value) != {'host', 'port'}:
        raise ValueError('web_settings_invalid')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise ValueError('web_port_invalid')
    return WebConfig(value['host'], value['port'])


def read_web(path=WEB_CONFIG):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_web_settings')
    value = private_json(path)
    info = path.stat()
    if os.name == 'posix' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('unsafe_web_settings')
    return validate_web(value)


def write_web(value, path=WEB_CONFIG):
    value = value if isinstance(value, WebConfig) else validate_web(value)
    path = Path(path)
    if path.exists() or path.is_symlink():
        read_web(path)
    atomic_json(path, web_document(value))
    return value


def migrate_state(value):
    if not isinstance(value, dict) or type(value.get('schema')) is not int:
        raise ValueError('invalid_service_state')
    if value['schema'] == 1 and set(value) == {'schema', 'bot_enabled'}:
        value = dict(schema=2, bot_enabled=value['bot_enabled'], web_enabled=True)
    if (value.get('schema') != 2 or set(value) != {'schema', 'bot_enabled', 'web_enabled'}
            or type(value['bot_enabled']) is not bool or type(value['web_enabled']) is not bool):
        raise ValueError('invalid_service_state')
    return dict(value)
