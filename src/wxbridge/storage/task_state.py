"""Fail-closed queue state invariants shared by writers and read-only inspection."""
import sqlite3


PHASES = frozenset(('open', 'queued', 'external', 'done'))


def validate_task(task):
    if (not isinstance(task, dict) or not isinstance(task.get('phase'), str)
            or task['phase'] not in PHASES or 'intent' not in task
            or (task['intent'] is not None and not isinstance(task['intent'], dict))
            or (task['phase'] == 'done' and task['intent'] is not None)):
        raise ValueError('queue_invalid_task_state')


def validate_task_states(db):
    """Check all persisted rows once on open, including completed records.

    CASE short-circuits malformed JSON before accessing its fields. No task body is
    returned or decoded, and callers must not interpret an unknown state as idle.
    """
    try:
        invalid = db.execute("""SELECT 1 FROM tasks WHERE CASE
            WHEN json_valid(data)=0 THEN 1
            WHEN json_type(data) IS NOT 'object' THEN 1
            WHEN json_type(data,'$.phase') IS NOT 'text' THEN 1
            WHEN json_extract(data,'$.phase') NOT IN ('open','queued','external','done') THEN 1
            WHEN json_type(data,'$.intent') IS NULL THEN 1
            WHEN json_type(data,'$.intent') NOT IN ('null','object') THEN 1
            WHEN json_extract(data,'$.phase')='done' AND json_type(data,'$.intent')='object' THEN 1
            ELSE 0 END=1 LIMIT 1""").fetchone()
    except sqlite3.DatabaseError:
        raise ValueError('queue_invalid_task_state') from None
    if invalid is not None:
        raise ValueError('queue_invalid_task_state')
