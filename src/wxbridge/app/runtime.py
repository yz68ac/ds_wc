"""生产入口：规则、单实例锁及可恢复队列生命周期。"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from pathlib import Path

from wxbridge.app.bot import GroupSession, setup_logging
from wechat_linux.runtime.locking import instance_lock
from wxbridge.instances import runtime_locks


def run_bot(cfg, *, once=False, echo=False):
    cfg = copy.deepcopy(cfg)
    if cfg.get("source") != "db":
        raise ValueError("当前运行链路只使用 db 消息源")
    if cfg.get('instances') and not cfg['ui']['dry_run']:
        if cfg['ui']['backend'] != 'native_service':
            raise ValueError('多群发送要求 ui.backend=native_service')
        for entry in cfg['instances']:
            vision = entry['config']['vision']
            if entry['enabled'] and vision['download_enabled'] and vision['download_backend'] != 'native':
                raise ValueError('多群图片下载要求 vision.download_backend=native')
    active_configs = [cfg, *[e['config'] for e in cfg.get('instances', [])]]
    for active in active_configs:
        active['ui']['dry_run'] = cfg['ui']['dry_run']
        active["target"]["trigger"].update(require_mention_metadata=True, reply_all=False, prefixes=[])
        active["ui"]["verify_chat_selected"] = True
        active["reply_to_backlog"] = False
        if active["ui"].get("dry_run"):
            active["queue"]["file"] += ".dry-run"
            active["state_file"] = str(Path(active["state_file"]).parent / "dry-run" / "state.json")
    with ExitStack() as stack:
        for path in runtime_locks(cfg):
            stack.enter_context(instance_lock(path))
        setup_logging(cfg, verbose=False)
        if cfg.get('instances'):
            from wxbridge.app.coordinator import Coordinator
            bot = Coordinator(cfg, echo=echo)
        else:
            from wxbridge.instances import queue_candidates
            from wxbridge.storage.journal import open_readonly
            for path in queue_candidates(cfg):
                if path == Path(cfg['queue']['file']) or not path.exists():
                    continue
                inactive = open_readonly(str(path))
                try:
                    if any(t.get('intent') for t in inactive.tasks()):
                        raise ValueError('unconfirmed_instance_send: 请重新启用所属实例核对发送')
                finally:
                    inactive.close()
            bot = GroupSession(cfg, echo=echo)
        try:
            bot.save_status()
            bot.run(once=once)
        finally:
            bot.close()
    return 0
