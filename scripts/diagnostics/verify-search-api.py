"""Explicit model probe using group-owned clients; never sends WeChat messages."""
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
from wxbridge.config import load_config
from wxbridge.app.model_runtime import GroupModels

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live-api',action='store_true')
    parser.add_argument('--config',default=str(ROOT/'config.json'))
    args=parser.parse_args()
    if not args.live_api: parser.error('Explicit --live-api is required')
    cfg=load_config(args.config)
    runtime=GroupModels(cfg)
    try:
        cfg['search']['enabled']=True
        runtime.cfg['search']['enabled']=True
        job=runtime.submit('generate',(cfg,[dict(role='system',content='请查询必要的公开资料。'),dict(role='user',content='查询 DeepSeek 官方 API 文档地址。')],None),timeout=cfg['search']['total_timeout_seconds'])
        import time
        while (result:=job.poll()) is None: time.sleep(.05)
        print('ok' if result['ok'] else result['error'])
        return 0 if result['ok'] else 1
    finally: runtime.close()
if __name__=='__main__': raise SystemExit(main())
