"""Private system service entry; human-facing control lives in wxbridge CLI."""
import argparse
import importlib.util
import logging
import os
from pathlib import Path
import signal


def main(argv=None):
    parser = argparse.ArgumentParser(description='WXBridge service supervisor')
    parser.add_argument('--root', type=Path, default=Path('/opt/wxbridge'))
    parser.add_argument('--web-host', default='127.0.0.1')
    parser.add_argument('--web-port', type=int, default=8730)
    args = parser.parse_args(argv)
    if not hasattr(os,'geteuid') or os.geteuid() != 0:
        parser.error('The supervisor must run as root; business children drop privileges.')
    root = args.root.resolve()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    spec = importlib.util.spec_from_file_location('boot_recovery',root/'scripts/boot-recovery.py')
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    recovery.runtime()
    from wxbridge_service.supervisor import Supervisor
    supervisor = Supervisor(root,web_host=args.web_host,web_port=args.web_port)
    def stop(_signal,_frame):
        supervisor.stopping = True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    supervisor.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
