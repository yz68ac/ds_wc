"""CLI prints metadata only; text is accepted from stdin or a UTF-8 file."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from wxbridge.native.client import NativeClient
from wxbridge.native.protocol import NativeError, settings


def command(cfg,args):
    options=settings(cfg)
    if not options['enabled']:raise NativeError('native_api_disabled')
    client=NativeClient(options['socket'])
    if args.action=='info':value=client.info()
    elif args.action=='status':
        if not args.request_id:raise NativeError('request_id_required')
        value=client.get_status(args.request_id)
    else:
        if not args.request_id or not args.chat_id:raise NativeError('request_id_and_chat_id_required')
        if args.text_file:
            with Path(args.text_file).open('rb') as stream:body=stream.read(options['max_text_bytes']+1)
        else:body=sys.stdin.buffer.read(options['max_text_bytes']+1)
        if len(body)>options['max_text_bytes']:raise NativeError('text_too_large')
        try:text=body.decode('utf-8-sig')
        except UnicodeError:raise NativeError('invalid_text') from None
        value=client.send_text(args.chat_id,text,args.request_id)
    print(json.dumps(value,ensure_ascii=False))
    return 0
