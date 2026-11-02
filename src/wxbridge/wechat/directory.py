"""One-shot metadata reader. No sender, message query, key scanner or disk cache."""
import argparse
import json
from pathlib import Path

def read_directory(config):
    from wxbridge.config import load_account
    from wxbridge.wechat.store import WeChatStore
    cfg=load_account(config)
    if not cfg.db_dir:raise ValueError('account_unbound')
    store=WeChatStore(cfg.db_dir,cfg.keys_file,cfg.decrypted_dir)
    result=store.contact_directory(limit=10000)
    for item in result['items']:
        item['nickname']=item['nickname'][:128]
        item['remark']=item['remark'][:128]
    name=Path(store.db_dir).parent.name
    selves={name,name.rsplit('_',1)[0]}
    result['items']=[item for item in result['items'] if item['id'] not in selves]
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:result=read_directory(args.config)
    except Exception:
        print(json.dumps(dict(error='contacts_unavailable')))
        return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
