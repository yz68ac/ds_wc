"""Verify the package files actually imported by an installed application."""
import hashlib
from importlib.util import find_spec
from pathlib import Path

PACKAGES = ('wxbridge', 'wxbridge_cli', 'web', 'provider', 'wechat_linux')


def installed_mismatches(files, *, locations=None, data_root=None):
    if locations is None:
        locations = {}
        for package in PACKAGES:
            spec = find_spec(package)
            if spec and spec.origin:
                locations[package] = Path(spec.origin).parent
    problems = []
    for name, info in files.items():
        parts = Path(name).parts
        if name == 'config.defaults.json' or name.startswith('src/web/frontend/dist/'):
            relative = name if name == 'config.defaults.json' else 'webui/'+name[len('src/web/frontend/dist/'):]
            if data_root is not None:
                path = Path(data_root)/relative
            else:
                from wxbridge.paths import resource_path, frontend_path
                path = resource_path(name) if name == 'config.defaults.json' else frontend_path()/name[len('src/web/frontend/dist/'):]
        elif len(parts) >= 3 and parts[0] == 'src' and parts[1] in PACKAGES and Path(name).suffix in ('.py', '.js', '.json', '.c'):
            root = locations.get(parts[1])
            path = Path(root).joinpath(*parts[2:]) if root else None
        else:
            continue
        if path is None or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            problems.append(name)
    return problems
