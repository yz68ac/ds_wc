"""Stopped-listener diagnostic: metadata only; never calls API or sends messages."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from wxbridge.app.sessions.group import GroupSession
from wxbridge.config import load_config
from wxbridge.instances import select_instance
from wxbridge.app.execution.locks import instance_lock


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance')
    parser.add_argument('after', nargs='?', type=int)
    args=parser.parse_args(argv)
    app=load_config(ROOT/'config.json')
    identifier=args.instance or (next(iter(app.instances)) if len(app.instances)==1 else None)
    if identifier is None:parser.error('--instance is required when there is not exactly one configured instance')
    cfg=select_instance(app,identifier)
    with ExitStack() as stack:
        for name in sorted({cfg.paths.queue_file+".lock", str(Path(cfg.account.decrypted_dir)/".listener.lock")}):
            stack.enter_context(instance_lock(name))
        bot = GroupSession(cfg, instance_id=cfg.id or None)
        try:
            now = time.time()
            after = args.after if args.after is not None else max(0, bot.last_local_id-12)
            rows = bot.store.messages_after(bot.chat_id, after, limit=20)
            info = []
            for msg in rows:
                info.append(dict(local_id=msg["local_id"], type=msg["type"], sent=msg["time"],
                                 age_seconds=round(now-msg["time"], 3), own=msg["sender"]==bot.store.self_wxid,
                                 sender_known=bool(msg["sender"]), chars=len(msg["text"]),
                                 mention_metadata=msg["mentioned_users"] is not None,
                                 mentions=len(msg["mentioned_users"] or []), self_mentioned=bot.store.self_wxid in (msg["mentioned_users"] or []),
                                 text_has_at="@" in msg["text"], accepted_now=bot._accept(msg, now)))
                if msg["type"] > 4294967295:
                    import xml.etree.ElementTree as ET
                    try:
                        tree = ET.fromstring(msg["text"])
                        app = tree.find(".//appmsg")
                        info[-1]["xml"] = {"root": tree.tag, "tags": sorted({n.tag for n in tree.iter()}),
                                            "appmsg_type": app.findtext("type") if app is not None else None,
                                            "title_chars": len(app.findtext("title") or "") if app is not None else None}
                        ref = app.find("refermsg") if app is not None else None
                        if ref is not None:
                            info[-1]["xml"]["refermsg_type"] = ref.findtext("type")
                            info[-1]["xml"]["refermsg_fields"] = [{"tag": n.tag, "chars": len(n.text or ""), "children": len(n)} for n in ref]
                    except ET.ParseError:
                        info[-1]["xml"] = {"valid": False}
            print(json.dumps({"now": now, "cursor": bot.last_local_id, "messages": info}, ensure_ascii=False, indent=2))
        finally:
            bot.close()


if __name__ == "__main__":
    main()
