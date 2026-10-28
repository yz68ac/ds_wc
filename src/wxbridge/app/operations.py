"""Shared metadata paths for boot recovery and release checks, without readers."""
from pathlib import Path

from wxbridge.config import load_config


def configured_groups(app, runtime):
    app, runtime = Path(app), Path(runtime)
    if not (app/'config.json').exists():
        return [(None, dict(queue=dict(file=str(runtime/'queue.sqlite3')), state_file=str(runtime/'state.json')), True)]
    cfg = load_config(app/'config.json')
    if not cfg.get('instances'):
        return [(None, cfg, True)]
    return [(e['id'], e['config'], e['reuse_legacy_state']) for e in cfg['instances'] if e['enabled']]


def live_status_path(app, runtime):
    groups = configured_groups(app, runtime)
    if groups[0][0] is None:
        return Path(groups[0][1]['state_file']).parent/'live-status.json'
    cfg = load_config(Path(app)/'config.json')
    return Path(cfg['state_file']).parent/'account-status.json'


def queue_paths(app, runtime):
    from wxbridge.instances import queue_candidates
    groups = configured_groups(app, runtime)
    active = {Path(cfg['queue']['file']): identifier for identifier, cfg, _ in groups}
    cfg = load_config(Path(app)/'config.json') if (Path(app)/'config.json').exists() else groups[0][1]
    for path in queue_candidates(cfg):
        if path.exists() and path not in active:
            active[path] = 'inactive:'+path.parent.name
    return active
