"""本次从旧版迁移用：监听已停时初始化队列，清空获授权的 41 字旧草稿。"""
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxbridge.config import load_config
from wxbridge.storage.journal import Journal
from wxbridge.app.runtime import instance_lock
from wxbridge.wechat.store import WeChatStore
from wxbridge.wechat.ui import A11y


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root/"config.json")
    cutover = Path(cfg["state_file"]).parent / "queue-cutover.json"
    if not cutover.exists():
        raise ValueError("缺少已记录的切换游标，禁止建立当前消息基线")
    with instance_lock(str(Path(cfg["decrypted_dir"])/".listener.lock")), instance_lock(cfg["queue"]["file"]+".lock"):
        store = WeChatStore(cfg["db_dir"], cfg["keys_file"], cfg["decrypted_dir"])
        chat_id, title = store.resolve_chat(cfg["target"]["chat"])
        if not store.self_wxid or not chat_id.endswith("@chatroom"):
            raise ValueError("账号或群无法确认")
        cursor = json.loads(cutover.read_text())["chats"][chat_id]["last_local_id"]
        journal = Journal(cfg["queue"]["file"], {"account": store.self_wxid, "chat": chat_id}, cursor)
        try:
            print(json.dumps({"migration_cursor": journal.get("cursor"),
                              "draft_cleanup": A11y(cfg["ui"]).clear_authorized_draft(title, 41)}), flush=True)
        finally:
            journal.close()
    # 一次性维护步骤；保留脱敏游标记录作为审计，不作为日常启动路径。


if __name__ == "__main__":
    main()
