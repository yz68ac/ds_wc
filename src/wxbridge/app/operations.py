"""Shared metadata paths for boot recovery and release checks, without readers."""
from pathlib import Path

from wxbridge.config import load_config


def configured_groups(app, runtime):
    app, runtime = Path(app), Path(runtime)
    cfg = load_config(app/'config.json')
    return [(e['id'], e['config'], False) for e in cfg['instances'] if e['enabled']]


def live_status_path(app, runtime):
    cfg = load_config(Path(app)/'config.json')
    return Path(cfg['state_file']).parent/'account-status.json'


def queue_paths(app, runtime):
    from wxbridge.instances import queue_candidates
    groups = configured_groups(app, runtime)
    active = {Path(cfg['queue']['file']): identifier for identifier, cfg, _ in groups}
    cfg = load_config(Path(app)/'config.json')
    for path in queue_candidates(cfg):
        if path.exists() and path not in active:
            active[path] = 'inactive:'+path.parent.name
    return active
