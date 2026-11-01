"""Private system service entry; human-facing control lives in wxbridge CLI."""
import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess


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
    # Recovery deliberately imports a minimal package projection through SCANNER.
    # Keep those sys.path/sys.modules changes outside the long-lived supervisor.
    subprocess.run(['/usr/bin/python3', str(root/'scripts/boot-recovery.py'), 'runtime'], check=True)
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
