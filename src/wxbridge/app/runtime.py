"""生产入口：规则、单实例锁及可恢复队列生命周期。"""
from __future__ import annotations
from wxbridge.config import AppConfig

from contextlib import ExitStack
from pathlib import Path

from wxbridge.app.status.logging_context import setup_logging
from wxbridge.app.execution.locks import instance_lock
from wxbridge.instances import runtime_locks


def run_bot(cfg: AppConfig, *, once=False, echo=False):
    from wxbridge.config import prepare_runtime
    cfg = prepare_runtime(cfg)
    if cfg.account.source != 'db':
        raise ValueError('当前运行链路只使用 db 消息源')
    if not any(value.enabled for value in cfg.instances.values()):
        raise ValueError('no_enabled_instances: configure instance/<id>/config.json')
    if not cfg.account.ui.dry_run:
        if cfg.account.ui.backend != 'native_service':
            raise ValueError('多群发送要求 ui.backend=native_service')
        for entry in cfg.instances.values():
            if entry.enabled and entry.vision.download_enabled and entry.vision.download_backend != 'native':
                raise ValueError('多群图片下载要求 vision.download_backend=native')
    with ExitStack() as stack:
        for path in runtime_locks(cfg):
            stack.enter_context(instance_lock(path))
        setup_logging(cfg, verbose=False)
        from wxbridge.app.sessions.coordinator import Coordinator
        bot = Coordinator(cfg, echo=echo)
        try:
            bot.save_status()
            bot.run(once=once)
        finally:
            bot.close()
    return 0


def main(argv=None):
    """Private foreground entry used by the supervisor; no lifecycle controls."""
    import argparse
    from wxbridge.config import load_config
    parser = argparse.ArgumentParser(description='WXBridge foreground worker')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    from wxbridge.config import prepare_runtime
    return run_bot(prepare_runtime(cfg, dry_run=not args.send))


if __name__ == '__main__':
    raise SystemExit(main())
