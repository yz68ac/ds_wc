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
            self.db.execute('BEGIN IMMEDIATE')
            self._migrate_tasks()
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

    def _migrate_tasks(self):
        """一次迁移旧队列；只增加检索列，正文和未决发送原样保留。"""
        columns = {r[1] for r in self.db.execute('PRAGMA table_xinfo(tasks)')}
        if 'phase' not in columns:
            # 生成列保留旧版 INSERT VALUES(id,data) 的兼容性；索引由 SQLite 自动维护。
            for name, definition in (('phase', "TEXT AS (json_extract(data,'$.phase')) VIRTUAL"),
                    ('has_intent', "INTEGER AS (json_type(data,'$.intent')='object') VIRTUAL"),
                    ('optional', "INTEGER AS (COALESCE(json_extract(data,'$.trigger_kind')='proactive',0)) VIRTUAL"),
                    ('deadline', "REAL AS (json_extract(data,'$.deadline')) VIRTUAL")):
                self.db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
        self.db.execute("CREATE INDEX IF NOT EXISTS active_tasks ON tasks(has_intent DESC, optional, deadline, id) WHERE phase != 'done'")
        self.db.execute('CREATE INDEX IF NOT EXISTS completed_tasks ON tasks(phase, id)')
        maximum = self.db.execute('SELECT COALESCE(MAX(id),0) FROM tasks').fetchone()[0]
        self.set('task_sequence', max(maximum, self.get('task_sequence', 0)))

    def _next_task_id(self):
        identifier = self.get('task_sequence', 0)+1
        self.set('task_sequence', identifier)
        return identifier

    def prune_completed(self, keep: int, protected=()):
        """仅裁剪已完成行；单调序号防止已删除的任务 ID 被复用。"""
        with self.db:
            sql = ("DELETE FROM tasks WHERE phase='done' AND id NOT IN "
                   "(SELECT id FROM tasks WHERE phase='done' ORDER BY id DESC LIMIT ?)")
            if protected:
                sql += ' AND id NOT IN ('+','.join('?' for _ in protected)+')'
            self.db.execute(sql, (keep, *protected))

    def close(self):
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def tasks(self, *, unfinished: bool = True) -> list[dict[str, Any]]:
        if getattr(self, 'legacy_readonly', False):
            tasks = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM tasks ORDER BY id')]
            return [t for t in tasks if not unfinished or t['phase'] != 'done']
        sql = "SELECT data FROM tasks" + (" INDEXED BY active_tasks WHERE phase != 'done'" if unfinished else '') + ' ORDER BY id'
        return [json.loads(row[0]) for row in self.db.execute(sql)]

    def task(self, task_id: int):
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, task):
        self.db.execute("INSERT OR REPLACE INTO tasks (id,data) VALUES (?,?)",
                        (task['id'], json.dumps(task, ensure_ascii=False)))

    def recover(self, now: float):
        with self.db:
            for job in self.memory_jobs():
                if job["state"] == "running":
                    job["state"] = "pending"
                    self.put_memory(job)
            for task in self.tasks():
                if task["generation"] == "running":
                    task["generation"] = "pending"
                if task['generation'] == 'failed' and task['blocked_reason'].startswith('generation:'):
                    task.update(generation='pending', blocked_reason='', generation_attempts=1,
                                generation_retry_at=0)
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
                        task = dict(id=self._next_task_id(), sender=msg["sender"], phase="open", version=0, trigger_kind=kind,
                                    triggers=[], trigger_ids=[], generation="pending", chunks=[], next_chunk=0,
                                    intent=None, blocked_reason="", closed_at=None, accepted_at=now)
                        tasks.append(task)
                    task["triggers"].append(msg)
                    if (task.get("notice") or {}).get("state") == "pending" and not task["intent"]:
                        task.pop("notice", None)  # 仍在合并窗口的旧版本尚未发出的提示作废。
                    task.setdefault("trigger_ids", []).append(msg["local_id"])
                    task.update(version=task["version"] + 1, deadline=now + (0 if kind == "pat" else debounce),
                                generation="pending", chunks=[], blocked_reason="", generation_attempts=0,
                                generation_retry_at=0, generation_failed=False, last_generation_error='')
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
        if getattr(self, 'legacy_readonly', False):
            tasks = sorted(self.tasks(), key=lambda t: (not bool(t.get('intent')),
                t.get('trigger_kind') == 'proactive', t['deadline'], t['id']))
            return tasks[0] if tasks else None
        row = self.db.execute("SELECT data FROM tasks WHERE phase != 'done' "
            'ORDER BY has_intent DESC, optional, deadline, id LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

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
            identifier = self._next_task_id()
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

    def start_generation(self, task_id, version, now):
        with self.db:
            task = self.task(task_id)
            if not task or task['version'] != version or task['phase'] == 'done':
                return False
            task.update(generation='running', generation_started_at=now,
                        generation_attempts=task.get('generation_attempts', 0)+1)
            self.put(task)
        return True

    def generation_failure(self, task_id, version, code, now, *, retryable, retries, delay, fallback):
        with self.db:
            task = self.task(task_id)
            if not task or task['version'] != version or task['phase'] == 'done':
                return False
            retry = retryable and task.get('generation_attempts', 0) <= retries
            # 生成失败不能清除发送侧的未知状态；意图仍由发出记录核对。
            task.update(generation='pending' if retry else 'ready',
                        generation_retry_at=now+delay if retry else 0,
                        generation_failed=not retry, last_generation_error=code,
                        chunks=[] if retry else [fallback])
            if task['blocked_reason'].startswith('generation:'):
                task['blocked_reason'] = ''
            if not retry and not task.get('intent') and (task.get('notice') or {}).get('state') == 'pending':
                task.pop('notice', None)
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
            if kind == 'reply' and 'first_submit_at' not in task:
                task.update(first_submit_at=now, queue_wait_seconds=max(0, now-task.get('accepted_at', now)))
            self.put(task)

    def acknowledge(self, task_id: int, local_id: int, now: float, *, memory=False, on_confirm=None):
        with self.db:
            task = self.task(task_id)
            if not task["intent"]:
                return
            task['last_confirmation_seconds'] = max(0, now-task['intent'].get('created_at', now))
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
                if on_confirm and not task.get('generation_failed'):
                    changes = on_confirm(task, local_id, now, self.get('proactive_session'),
                                         self.get('proactive_entries', []))
                    if set(changes)-{'proactive_session', 'proactive_entries'}:
                        raise ValueError('invalid_confirmation_updates')
                    for key, value in changes.items():
                        self.set(key, value)
                if memory and task.get('trigger_kind') != 'proactive' and not task.get('generation_failed'):
                    # 与最终分片确认同事务；只留触发指针，主进程随后重读原窗口。
                    self.enqueue_memory('reply', 'reply:'+str(task_id),
                        [dict(local_id=m['local_id'], time=m['time']) for m in task['triggers']])
                # 不长期保留已完成的群聊正文。
                task.update(phase="done", triggers=[], chunks=[])
            self.put(task)

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
                                generation_attempts=t.get('generation_attempts', 0),
                                generation_retry_at=t.get('generation_retry_at', 0),
                                generation_failed=t.get('generation_failed', False),
                                last_generation_error=t.get('last_generation_error', ''),
                                queue_wait_seconds=t.get('queue_wait_seconds'),
                                last_confirmation_seconds=t.get('last_confirmation_seconds'),
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
        journal.legacy_readonly = 'phase' not in {r[1] for r in db.execute('PRAGMA table_xinfo(tasks)')}
        return journal.status()
    finally:
        db.close()
