"""主进程独占的内存 SQLite 队列；游标与接纳任务在同一事务提交。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

from wxbridge.storage.memoryfs import require_memory_directory
from wxbridge.wechat.pats import is_pat


def checked_path(value: str, *, existing: bool = False) -> Path:
    path = Path(value).absolute()
    if path.is_symlink():
        raise ValueError("队列文件不能是符号链接")
    require_memory_directory(str(path.parent))
    if path.exists():
        info = path.stat()
        if not path.is_file() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("队列文件必须归当前用户所有，权限 0600")
    elif existing:
        raise ValueError("队列尚未建立")
    return path


class Journal:
    def __init__(self, path: str, identity: dict[str, str], baseline: int):
        target = checked_path(path)
        fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self.db = sqlite3.connect(target)
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_jobs (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        """)
        saved = self.get("identity")
        if saved is not None and saved != identity:
            self.db.close()
            raise ValueError("队列的微信账号或目标群不匹配，拒绝恢复")
        with self.db:
            if saved is None:
                self.set("identity", identity)
                self.set("cursor", baseline)
                self.set("last_success", 0)
            for row in self.db.execute('SELECT id,data FROM memory_jobs').fetchall():
                job = json.loads(row[1])
                if 'origin' not in job:
                    job.update(origin='reply', key='reply:'+str(row[0]))
                    self.put_memory(job)
            maximum = self.db.execute('SELECT COALESCE(MAX(id),0) FROM memory_jobs').fetchone()[0]
            self.set('memory_sequence', max(maximum, self.get('memory_sequence', 0)))

    def close(self):
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def tasks(self, *, unfinished: bool = True) -> list[dict[str, Any]]:
        items = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM tasks ORDER BY id")]
        return [t for t in items if not unfinished or t["phase"] != "done"]

    def task(self, task_id: int):
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, task):
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)",
                        (task["id"], json.dumps(task, ensure_ascii=False)))

    def recover(self, now: float):
        with self.db:
            for job in self.memory_jobs():
                if job["state"] == "running":
                    job["state"] = "pending"
                    self.put_memory(job)
            for task in self.tasks():
                if task["generation"] == "running":
                    task["generation"] = "pending"
                if task["intent"]:
                    task["intent"]["deadline"] = now + task["intent"]["confirm_timeout"]
                    task["intent"]["recovered"] = True
                self.put(task)

    def intake(self, messages: list[dict], accept: Callable[[dict], bool], now: float,
               debounce: float, describe: Callable[[dict], dict] | None = None):
        """accept 抛异常时整个事务回滚，包括游标；普通聊天只推进游标。"""
        events = []
        with self.db:
            cursor = self.get("cursor", 0)
            tasks = self.tasks()
            for msg in messages:
                if msg["local_id"] <= cursor:
                    continue
                if describe:
                    events.append(describe(msg))
                if accept(msg):
                    kind = "pat" if is_pat(msg) else "mention"
                    task = next((t for t in tasks if kind == "mention" and t.get("trigger_kind", "mention") == kind
                                 and debounce > 0 and t["phase"] == "open"
                                 and t["sender"] == msg["sender"]
                                 and (now < t["deadline"] or msg["time"] < t["deadline"])), None)
                    if task is None:
                        row = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM tasks").fetchone()
                        task = dict(id=row[0], sender=msg["sender"], phase="open", version=0, trigger_kind=kind,
                                    triggers=[], trigger_ids=[], generation="pending", chunks=[], next_chunk=0,
                                    intent=None, blocked_reason="", closed_at=None)
                        tasks.append(task)
                    task["triggers"].append(msg)
                    if (task.get("notice") or {}).get("state") == "pending" and not task["intent"]:
                        task.pop("notice", None)  # 仍在合并窗口的旧版本尚未发出的提示作废。
                    task.setdefault("trigger_ids", []).append(msg["local_id"])
                    task.update(version=task["version"] + 1, deadline=now + (0 if kind == "pat" else debounce),
                                generation="pending", chunks=[], blocked_reason="")
                    self.put(task)
                cursor = max(cursor, msg["local_id"])
            self.set("cursor", cursor)
            if events:
                self.set("recent_intake", (self.get("recent_intake", []) + events)[-50:])
        return events

    def seal(self, now: float):
        with self.db:
            for task in self.tasks():
                if task["phase"] == "open" and now >= task["deadline"]:
                    task.update(phase="queued", closed_at=task["deadline"])
                    self.put(task)

    def head(self):
        # 未封批的较早窗口也占位，避免轮询/恢复时晚封批而插队。
        tasks = sorted(self.tasks(), key=lambda t: (not bool(t.get('intent')),
            t.get('trigger_kind') == 'proactive', t['deadline'], t['id']))
        return tasks[0] if tasks else None

    def request_notice(self, task_id: int, version: int, text: str) -> bool:
        with self.db:
            task = self.task(task_id)
            if not task or task["version"] != version or task["phase"] == "done" or task.get("notice"):
                return False
            task["notice"] = dict(text=text, state="pending")
            self.put(task)
        return True

    @staticmethod
    def outgoing_text(task):
        if task["intent"].get("kind") == "notice":
            return task["notice"]["text"]
        return task["chunks"][task["intent"]["part"]]

    def enqueue_proactive(self, text, through, now, expires, *, metadata=None):
        with self.db:
            if self.tasks():
                return None
            identifier = self.db.execute('SELECT COALESCE(MAX(id),0)+1 FROM tasks').fetchone()[0]
            task = dict(id=identifier, sender='', phase='queued', version=1, trigger_kind='proactive',
                triggers=[], trigger_ids=[], generation='ready', chunks=[text], next_chunk=0,
                intent=None, blocked_reason='', closed_at=now, deadline=now,
                proactive=dict(snapshot_id=through, expires_at=expires))
            self.put(task)
            if metadata:
                task['proactive'].update(metadata)
                self.put(task)
            return task

    def discard_proactive(self, identifier, reason):
        with self.db:
            task = self.task(identifier)
            if task and task.get('trigger_kind') == 'proactive' and not task['intent']:
                task.update(phase='done', chunks=[], triggers=[], blocked_reason='', cancelled=reason)
                self.put(task)

    def generated(self, task_id: int, version: int, chunks: list[str], error: str = "", *, vision=None, search=None) -> bool:
        with self.db:
            task = self.task(task_id)
            if not task or task["version"] != version or task["phase"] == "done":
                return False
            task.update(generation="failed" if error else "ready", chunks=chunks,
                        blocked_reason=error)
            if vision is not None:
                task["vision"] = vision  # 仅格式、分辨率、质量、数量和错误码；无路径、图片数据或密钥。
                self.set("last_vision", dict(task_id=task_id, **vision))
            if search is not None:
                task["search"] = search  # 仅数量、阶段和错误码，不含查询词、网页、URL 或正文。
                self.set("last_search", dict(task_id=task_id, **search))
            self.put(task)
        return True

    def intend(self, task_id: int, baseline: int, now: float, timeout: float, *, kind="reply"):
        with self.db:
            task = self.task(task_id)
            if task["intent"] is not None:
                raise ValueError("已有待确认发送，禁止重复提交")
            if kind not in ("reply", "notice") or kind == "notice" and (task.get("notice") or {}).get("state") != "pending":
                raise ValueError("发送意图类型或通知状态无效")
            task["intent"] = dict(kind=kind, part=-1 if kind == "notice" else task["next_chunk"], baseline=baseline, created_at=now,
                                  deadline=None, confirm_timeout=timeout, ui=None)
            self.put(task)

    def acknowledge(self, task_id: int, local_id: int, now: float, *, memory=False, proactive_config=None):
        with self.db:
            task = self.task(task_id)
            if not task["intent"]:
                return
            if task["intent"].get("kind") == "notice":
                task["notice"].update(state="confirmed", text="", ack_id=local_id)
                reason = task["blocked_reason"] if task["generation"] == "failed" else ""
                task.update(intent=None, blocked_reason=reason, last_notice_ack_id=local_id,
                            pre_submit_attempts=0, retry_at=0)
                self.set("last_success", now)
                self.put(task)
                return
            task["next_chunk"] += 1
            task.update(intent=None, blocked_reason="", last_ack_id=local_id)
            task.update(pre_submit_attempts=0, retry_at=0)
            self.set("last_success", now)
            if task.get('trigger_kind') == 'proactive':
                self.set('proactive', {**self.get('proactive', {}), 'outcome': 'sent',
                    'last_sent_at': now, 'last_ack_id': local_id, 'error': ''})
            if task["next_chunk"] == len(task["chunks"]):
                if proactive_config and proactive_config['enabled']:
                    self._conversation_confirmed(task, local_id, now, proactive_config)
                if memory and task.get('trigger_kind') != 'proactive':
                    # 与最终分片确认同事务；只留触发指针，主进程随后重读原窗口。
                    self.enqueue_memory('reply', 'reply:'+str(task_id),
                        [dict(local_id=m['local_id'], time=m['time']) for m in task['triggers']])
                # 不长期保留已完成的群聊正文。
                task.update(phase="done", triggers=[], chunks=[])
            self.put(task)

    def _conversation_confirmed(self, task, local_id, now, settings):
        """与发出确认同事务；只存消息指针、成员ID和预算，不复制聊天正文。"""
        data = task.get('proactive', {})
        optional = task.get('trigger_kind') == 'proactive'
        mode = data.get('mode', 'entry')
        if optional and mode == 'entry':
            entries = [t for t in self.get('proactive_entries', []) if 0 <= now-t < 3600]
            self.set('proactive_entries', entries+[now])
        if not settings['conversation']['enabled']:
            self.set('proactive_session', None)
            return
        participant = data.get('recipient') if optional else task['sender']
        if not participant:
            return  # 旧版短句缺少接话来源，不猜测成员。
        followups = 0
        if optional and mode == 'followup':
            previous = self.get('proactive_session')
            if not previous or previous['anchor_id'] != data['anchor_id']:
                return
            followups = previous['followups']+1
        if followups >= settings['conversation']['max_followups']:
            self.set('proactive_session', None)
            return
        self.set('proactive_session', dict(anchor_id=local_id, participant=participant,
            expires_at=now+settings['conversation']['window_seconds'], followups=followups,
            origin='entry' if optional and mode == 'entry' else 'reply' if not optional else previous['origin']))

    def memory_jobs(self):
        jobs = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM memory_jobs ORDER BY id")]
        return sorted(jobs, key=lambda j: ((j.get('window') or [0, j['triggers'][-1]['time']])[-1], j['id']))

    def enqueue_memory(self, origin, key, triggers, window=None):
        if any(j.get('key') == key for j in self.memory_jobs()):
            return
        identifier = self.get('memory_sequence', 0)+1
        self.set('memory_sequence', identifier)
        self.put_memory(dict(id=identifier, origin=origin, key=key, state='pending', retry_at=0,
                             attempts=0, triggers=triggers, **({'window':window} if window else {})))

    def put_memory(self, job):
        self.db.execute("INSERT OR REPLACE INTO memory_jobs VALUES (?,?)", (job["id"], json.dumps(job)))

    def finish_memory(self, job_id, now):
        with self.db:
            self.db.execute("DELETE FROM memory_jobs WHERE id=?", (job_id,))
            self.set("memory_last_update", now)
            self.set("memory_error", "")

    def status(self) -> dict:
        tasks = self.tasks()
        head = self.head()
        session = self.get('proactive_session') or {}
        return dict(queue_length=len(tasks), cursor=self.get("cursor", 0),
                    last_success=self.get("last_success", 0),
                    phase=("blocked" if head and head["blocked_reason"] else
                           "confirming" if head and head["intent"] else
                           head["phase"] if head else "listening"),
                    active_task=head["id"] if head else None,
                    active_version=head["version"] if head else None,
                    blocked_reason=head["blocked_reason"] if head else "",
                    recent_intake=self.get("recent_intake", [])[-20:],
                    last_vision=self.get("last_vision"),
                    last_search=self.get("last_search"),
                    proactive=self.get('proactive'),
                    conversation=dict(active=bool(session), expires_at=session.get('expires_at'),
                                      followups_sent=session.get('followups', 0)),
                    memory_pending=len(self.memory_jobs()), memory_error=self.get("memory_error", ""),
                    passive_learning=self.get('passive_learning'),
                    tasks=[dict(id=t["id"], version=t["version"], phase=t["phase"], trigger_kind=t.get('trigger_kind', 'mention'),
                                generation=t["generation"], questions=len(t["triggers"]),
                                next_chunk=t["next_chunk"], chunks=len(t["chunks"]),
                                blocked_reason=t["blocked_reason"], vision=t.get("vision"), search=t.get("search"),
                                memory_used=t.get("memory_used"),
                                notice_state=(t.get("notice") or {}).get("state"),
                                intent_kind=(t.get("intent") or {}).get("kind", "reply") if t.get("intent") else None) for t in tasks])


def read_status(path: str) -> dict:
    path = checked_path(path, existing=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA temp_store=MEMORY")
    journal = Journal.__new__(Journal)
    journal.db = db
    try:
        return journal.status()
    finally:
        db.close()
