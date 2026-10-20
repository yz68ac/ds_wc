"""离线测试共用的固定时间和合成消息；不读取实际聊天。"""

NOW = 1700000600


def msg(number, sent, text, sender="wxid_friend"):
    return {"local_id": number, "time": sent, "type": 1, "text": text,
            "sender": sender, "sender_name": sender, "time_source": "database"}
