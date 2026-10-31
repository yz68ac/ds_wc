"""离线测试共用的固定时间和合成消息；不读取实际聊天。"""

NOW = 1700000600


def msg(number, sent, text, sender="wxid_friend"):
    return {"local_id": number, "time": sent, "type": 1, "text": text,
            "sender": sender, "sender_name": sender, "time_source": "database"}


def history_page(rows, *, before, since, max_local_id, limit):
    from wechat_linux.storage.store import HistoryPage
    selected = sorted((m for m in rows if type(m.get('time')) is int
        and since <= m['time'] and (m['time'],m['local_id']) < before
        and m['local_id'] <= max_local_id),key=lambda m:(m['time'],m['local_id']))[-limit:]
    return HistoryPage(selected,len(selected),(selected[0]['time'],selected[0]['local_id']) if selected else None)
