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
        import base64, struct, zlib
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        pixels=(b'\0'+b'\xff\0\0'*64+b'\0\0\xff'*64)*64
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',128,64,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
        messages=[dict(role='user',content=[dict(type='text',text='图片左半边和右半边分别是什么颜色？'),dict(type='image_url',image_url=dict(url='data:image/png;base64,'+base64.b64encode(png).decode()))])]
        job=runtime.submit('request',(cfg,'reply',messages),timeout=30)
        import time
        while (result:=job.poll()) is None: time.sleep(.05)
        print('ok' if result['ok'] else result['error'])
        return 0 if result['ok'] else 1
    finally: runtime.close()
if __name__=='__main__': raise SystemExit(main())
