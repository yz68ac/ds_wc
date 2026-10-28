"""Deterministic time, expiry and evidence-selection rules; no model or file I/O."""
from datetime import datetime
from wxbridge.storage.member_references import ambiguous_fields

DAY = 86400


LIMITS = {"scene": 10, "event": 10, "preference": 5, "impression": 3}


TTL = {"scene": 6*3600, "event": 30*DAY, "preference": 90*DAY, "impression": 90*DAY}


def ambiguous_labels(entry):
    """Old summaries have no historical label map; never reinterpret it using today's map."""
    return ambiguous_fields(entry, ('summary',))


def usable_entries(person, now):
    return [e for e in active_entries(person, now) if not ambiguous_labels(e)]


def fuzzy_time(timestamp, now):
    days = (datetime.fromtimestamp(now).date()-datetime.fromtimestamp(timestamp).date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return "这几天"
    if days < 14:
        return "上周"
    return "前阵子"


def affinity_value(person, now):
    score = person.get("affinity", 50)
    anchor = person.get("decay_anchor", person.get("last_interaction", now))
    steps = max(0, int((now-anchor-14*DAY)//(7*DAY)))
    return score + min(steps, 50-score) if score < 50 else score-min(steps, score-50)


def active_entries(person, now):
    return [e for e in person.get("entries", []) if now-e["confirmed_at"] < TTL[e["kind"]]]
