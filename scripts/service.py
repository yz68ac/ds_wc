"""Unified service entry and local status/control CLI."""
import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description='WXBridge single-service lifecycle')
    parser.add_argument('action',choices=('run','status','start','stop','apply'))
    parser.add_argument('--web-host',default='127.0.0.1')
    parser.add_argument('--web-port',type=int,default=8730)
    args=parser.parse_args()
    if args.action!='run':
        from wxbridge_web.control import request
        print(json.dumps(request(args.action),ensure_ascii=False,indent=2))
        return
    if os.geteuid()!=0:
        parser.error('The supervisor must run as root; business children drop privileges.')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    spec=importlib.util.spec_from_file_location('boot_recovery',ROOT/'scripts/boot-recovery.py')
    recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
    recovery.runtime()
    from wxbridge_service.supervisor import Supervisor
    supervisor=Supervisor(ROOT,web_host=args.web_host,web_port=args.web_port)
    def stop(_signal,_frame):supervisor.stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    supervisor.run()


if __name__=='__main__':main()
