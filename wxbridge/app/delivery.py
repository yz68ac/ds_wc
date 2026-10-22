"""微信提交与数据库确认；只依赖发送所需的配置、存储和工作进程工厂。"""
from __future__ import annotations

import json
import logging
import time
from functools import partial
from wxbridge.app.conversation import confirmation_updates
from wxbridge.wechat.store import StoreError

log = logging.getLogger('wxbridge')


class DeliveryController:
    def __init__(self, cfg, journal, store, chat_id, chat_title):
        self.cfg, self.journal, self.store = cfg, journal, store
        self.chat_id, self.chat_title, self.job = chat_id, chat_title, None

    def poll_result(self):
        if self.job is None:
            return
        task_id, part, job = self.job
        result = job.poll()
        if result is None:
            return
        job.close()
        self.job = None
        task = self.journal.task(task_id)
        metadata = result.get("ui") or {"error": result.get("error", "UIError")}
        # 发出记录可能比 UI 读回先到达；即使已确认，也保留动作核验元数据。
        with self.journal.db:
            task["last_ui"] = metadata
            self.journal.put(task)
        log.info("发送操作 task=%s part=%s metadata=%s", task_id, part,
                 json.dumps(metadata, ensure_ascii=False))
        if not task["intent"] or task["intent"]["part"] != part:
            return
        operation = metadata.get("operation") or {}
        retryable = {"command_timeout", "command_failed", "activation_failed", "pointer_mismatch", "pointer_unknown", "button_obstructed"}
        if (not result["ok"] and operation.get("submitted") is False
                and metadata.get("error") in retryable and task.get("pre_submit_attempts", 0) < 2):
            with self.journal.db:
                task.update(intent=None, blocked_reason="", retry_at=time.time()+2,
                            pre_submit_attempts=task.get("pre_submit_attempts", 0)+1)
                self.journal.put(task)
            log.info("提交前失败，安全等待重试 task=%s part=%s attempt=%s", task_id, part, task["pre_submit_attempts"])
            return
        with self.journal.db:
            task["intent"]["deadline"] = time.time() + float(self.cfg["ui"]["send_confirm_timeout_seconds"])
            task["intent"]["ui"] = metadata
            self.journal.put(task)

    def confirm(self, *, memory=False):
        task = self.journal.head()
        if not task or not task["intent"]:
            return
        intent, cursor = task["intent"], task["intent"]["baseline"]
        text = self.journal.outgoing_text(task)
        while True:
            rows = self.store.messages_after(self.chat_id, cursor, limit=100)
            for msg in rows:
                if (msg["local_id"] > intent["baseline"] and msg.get("sender") == self.store.self_wxid
                        and msg.get("type") == 1 and msg.get("text") == text):
                    self.journal.acknowledge(task["id"], msg["local_id"], time.time(), memory=memory,
                        on_confirm=partial(confirmation_updates, settings=self.cfg.get('proactive')))
                    log.info("发送确认 task=%s part=%s local_id=%s", task["id"], intent["part"], msg["local_id"])
                    return
            if len(rows) < 100:
                break
            next_cursor = rows[-1]["local_id"]
            if next_cursor <= cursor:
                raise StoreError("发送确认分页未前进")
            cursor = next_cursor
        if intent["deadline"] is not None and time.time() >= intent["deadline"]:
            with self.journal.db:
                code = (intent.get("ui") or {}).get("error", "no_outbound_record")
                task["blocked_reason"] = "send_confirmation_timeout:" + code + ": 无匹配发出记录，暂停发送，继续接收"
                self.journal.put(task)

    def send(self, job_factory, validate=None):
        if self.job is not None:
            return
        task = self.journal.head()
        if not task or task["phase"] != "queued" or task["intent"] or task["blocked_reason"]:
            return
        notice = (task.get("notice") or {}).get("state") == "pending"
        if notice and not self.cfg.get('search', {}).get('feedback_enabled', False):
            # 关闭提示后不补发旧的未提交提示；已有意图在上面的分支等待数据库确认。
            with self.journal.db:
                task.pop('notice', None)
                self.journal.put(task)
            notice = False
        if not notice and task["generation"] != "ready":
            return
        now = time.time()
        if now < task.get("retry_at", 0):
            return
        if now - self.journal.get("last_success", 0) < float(self.cfg["target"].get("cooldown_seconds", 8)):
            return
        if validate and not validate(task, now):
            return
        if self.cfg["ui"].get("dry_run"):
            with self.journal.db:
                if notice:
                    task["notice"].update(state="confirmed", text="")
                else:
                    task.update(phase="done", triggers=[], chunks=[])
                self.journal.put(task)
            log.info("模拟完成 task=%s version=%s", task["id"], task["version"])
            return
        baseline = self.store.last_local_id(self.chat_id)
        timeout = float(self.cfg["ui"]["send_confirm_timeout_seconds"])
        self.journal.intend(task["id"], baseline, now, timeout, kind="notice" if notice else "reply")
        try:
            current = self.journal.task(task["id"])
            job = job_factory("send", (self.cfg, self.chat_title, self.journal.outgoing_text(current)), timeout=30)
            self.job = (task["id"], current["intent"]["part"], job)
            log.info("发送意图 task=%s version=%s kind=%s part=%s baseline=%s", task["id"], task["version"],
                     current["intent"]["kind"], current["intent"]["part"], baseline)
        except Exception as exc:
            with self.journal.db:
                current = self.journal.task(task["id"])
                current["intent"]["deadline"] = now + timeout
                current["intent"]["ui"] = {"error": type(exc).__name__}
                self.journal.put(current)

