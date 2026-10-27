"""Stopped-listener diagnostic: metadata only; never calls API or sends messages."""
from contextlib import ExitStack
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wxbridge.app.bot import WeChatDeepSeekBot
from wxbridge.config import load_config
from wxbridge.app.runtime import instance_lock


def main():
    cfg = load_config(ROOT/"config.json")
    with ExitStack() as stack:
        for name in sorted({cfg["queue"]["file"]+".lock", str(Path(cfg["decrypted_dir"])/".listener.lock")}):
            stack.enter_context(instance_lock(name))
        bot = WeChatDeepSeekBot(cfg)
        try:
            now = time.time()
            after = int(sys.argv[1]) if len(sys.argv) > 1 else max(0, bot.last_local_id-12)
            rows = bot.store.messages_after(bot.chat_id, after, limit=20)
            info = []
            for msg in rows:
                info.append(dict(local_id=msg["local_id"], type=msg["type"], sent=msg["time"],
                                 age_seconds=round(now-msg["time"], 3), own=msg["sender"]==bot.store.self_wxid,
                                 sender_known=bool(msg["sender"]), chars=len(msg["text"]),
                                 mention_metadata=msg["mentioned_users"] is not None,
                                 mentions=len(msg["mentioned_users"] or []), self_mentioned=bot._triggered(msg),
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
