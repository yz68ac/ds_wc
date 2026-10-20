"""按消息的实际发送时间选择上下文；观察时间不能替代发送时间。"""
from __future__ import annotations

from typing import Any


class ContextError(ValueError):
    pass


def message_time(message: dict[str, Any]) -> int | None:
    value = message.get("time")
    if message.get("time_source") in ("unknown", "observed"):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def context_bounds(cfg: dict[str, Any], trigger: dict[str, Any]) -> tuple[int, int]:
    end = message_time(trigger)
    if end is None:
        raise ContextError("触发消息缺少可靠的发送时间，取消回复；不能用读取时间代替")
    seconds = cfg["target"].get("context_window_seconds", 600)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ContextError("context_window_seconds 必须是正整数")
    return end - seconds, end


def select_context(
    cfg: dict[str, Any], context: list[dict[str, Any]], trigger: dict[str, Any]
) -> list[dict[str, Any]]:
    start, end = context_bounds(cfg, trigger)
    selected = []
    for message in context:
        sent = message_time(message)
        if sent is None or not start <= sent <= end:
            continue
        if message.get("type", 1) != 1 or not (message.get("text") or "").strip():
            continue
        if trigger.get("local_id") and message.get("local_id") == trigger["local_id"]:
            continue  # 触发消息在提示词末尾单独提供
        if (sent == end and trigger.get('local_id') and message.get('local_id')
                and message['local_id'] > trigger['local_id']):
            continue  # 秒级时间戳相同，仍排除排在触发消息之后的记录
        selected.append(message)
    return sorted(selected, key=lambda m: (m["time"], m.get("local_id") or 0))
