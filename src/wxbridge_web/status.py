"""Bounded read-only projections of existing heartbeat and event files."""
import time
from pathlib import Path

from wxbridge.app.monitor import read_json, safe_events
from wxbridge_web.services import BOT, SESSION


def overview(repository, services, operations, *, demo=False):
    cfg = repository.account()
    runtime = Path(cfg['state_file']).parent
    states = services.status()
    live, failure = read_json(runtime/'account-status.json')
    login, _ = read_json(runtime/'login-status.json')
    bot = states[BOT]
    now = time.time()
    timestamp = live.get('updated_at')
    age = now-timestamp if type(timestamp) in (int,float) else None
    fresh = (age is not None and -5 <= age < 10 and bot['state']=='active'
             and live.get('phase')!='stopped' and live.get('pid')==bot['pid'])
    desired = repository.config_hash()
    applied = live.get('build',{}).get('config_hash')
    entries, total, workers = [], 0, 0
    for item in repository.list():
        active_status=live.get('instances',{}).get(item['id'],{})
        status = {**live.get('inactive_queues',{}).get(item['id'],{}),**active_status}
        queued = status.get('queue_length',0)
        count = status.get('model_workers',0)
        lifecycle=live.get('instance_lifecycle',{}).get(item['id'],{})
        if fresh:
            instance_state=('stopping' if active_status.get('quiescing') else 'running') if active_status else lifecycle.get('state','stopped')
        elif bot['state']=='inactive':
            instance_state='waiting_service' if item['enabled'] else 'stopped'
        else:
            instance_state='unknown'
        total += queued if type(queued) is int else 0
        workers += count if type(count) is int else 0
        entries.append(dict(**item, queue_length=queued, model_workers=count,
            cursor=status.get('cursor'), blocked_reason=status.get('blocked_reason','') or ('存在待核对的发送记录' if status.get('unconfirmed') else ''),
            last_success=status.get('last_success'), channels=status.get('model_channels',{}),
            image_generation=status.get('image_generation', {}),
            context=status.get('context', {}), context_policy=status.get('context_policy', {}),
            snapshot_current=bool(fresh),runtime_state=instance_state,
            applied_revision=lifecycle.get('applied_revision'),
            config_pending=item['revision']!=lifecycle.get('applied_revision'),
            request_id=lifecycle.get('request_id'),error=lifecycle.get('error',''),active_models=status.get('models',{})))
    events, event_error = safe_events(cfg['log_file'])
    return dict(demo=demo, collected_at=now, services=states,
        runtime=dict(state=('listening' if live.get('instances') else 'idle') if fresh else 'stopped' if bot['state']=='inactive' else 'stale' if live else 'unavailable',
            heartbeat_age_seconds=max(0,round(age,1)) if age is not None else None,
            snapshot_current=fresh, snapshot_error=failure, queue_length=total, model_workers=workers,
            send_blocked=bool(live.get('send_blocked')), login_phase=login.get('phase','unknown'),
            config_pending=bool(desired and desired!=applied), config_valid=desired is not None),
        instances=entries, events=events, event_error=event_error,
        capabilities=dict(service_control=services.allow_control, login_actions=False, queue_actions=False,
                          instance_control=bool(bot['state']!='active' or fresh and live.get('features',{}).get('instance_control'))),
        operations=operations.snapshot())
