"""主进程独占接收及数据库；API、UI 使用可终止的工作进程。"""
from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time

from wxbridge.ai.context import context_bounds, message_time
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.output import format_reply
from wxbridge.ai.memory import member_labels, snapshot
from wxbridge.ai.context import select_context
from wxbridge.config import PROJECT_ROOT
from wxbridge.storage.person_memory import MemoryStore
from wxbridge.storage.journal import Journal
from wxbridge.wechat.messages import chunk_text, is_triggered, reply_chunks
from wxbridge.wechat.store import StoreError, WeChatStore
from wxbridge.wechat.pats import is_pat
from wxbridge.app.workers import ProcessJob
from wxbridge.app.proactive import ProactiveController
from wxbridge.app.learning import enqueue_if_active, eligible_messages

log = logging.getLogger("wxbridge")


def setup_logging(cfg, verbose=True):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in log.handlers[:]:
        handler.close()
        log.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        Path(cfg["log_file"]).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg["log_file"], maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass


class WeChatDeepSeekBot:
    def __init__(self, cfg, echo=False):
        self.cfg, self.echo = cfg, echo
        self.store = WeChatStore(cfg["db_dir"], cfg["keys_file"], cfg["decrypted_dir"])
        self.chat_id, self.chat_title = self.store.resolve_chat(cfg["target"]["chat"])
        if not self.chat_id.endswith("@chatroom") or not self.store.self_wxid:
            raise StoreError("必须确认当前微信账号及目标普通群聊")
        baseline = None
        if not cfg["ui"].get("dry_run"):
            try:
                state = json.loads(Path(cfg["state_file"]).read_text(encoding="utf-8"))
                baseline = state["chats"][self.chat_id]["last_local_id"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if baseline is None:
            baseline = self.store.last_local_id(self.chat_id)
        self.journal = Journal(cfg["queue"]["file"],
                               {"account": self.store.self_wxid, "chat": self.chat_id}, int(baseline))
        self.journal.recover(time.time())
        self.generators = {}
        self.ui_job = None
        self.memory, self.memory_job, self.memory_error = None, None, ""
        self.memory_pruned_at = 0
        if cfg.get("memory", {}).get("enabled") and not cfg["ui"].get("dry_run") and not echo:
            try:
                directory = Path(cfg["memory"]["directory"])
                if not directory.is_absolute():
                    directory = PROJECT_ROOT/directory
                self.memory = MemoryStore(directory, self.journal.get("identity"))
                self.memory.enable_learning(self.last_local_id, time.time(), cfg['memory']['learning']['enabled'])
                settings = cfg['memory']['learning']
                enabled = settings['enabled'] and settings['passive_enabled']
                state = self.journal.get('passive_learning')
                with self.journal.db:
                    if state is None or (enabled and not state.get('enabled', False)):
                        state = dict(started_after_id=self.last_local_id, checked_id=self.last_local_id,
                                     checked_at=0, enqueued_at=0)
                    state['enabled'] = enabled
                    self.journal.set('passive_learning', state)
            except Exception as exc:
                self.memory_error = type(exc).__name__
                log.error("记忆初始化失败 category=%s", self.memory_error)
        self.started_at = time.time()
        self.job_factory = ProcessJob
        self.status_path = Path(cfg["state_file"]).parent / "live-status.json"
        self.proactive = ProactiveController(self) if 'proactive' in cfg else None

    _chunk = staticmethod(chunk_text)

    @property
    def last_local_id(self):
        return self.journal.get("cursor", 0)

    def _triggered(self, msg):
        if is_pat(msg):
            return (self.cfg["target"]["trigger"].get("pat_enabled", True)
                    and msg["pat"]["target"] == self.store.self_wxid and msg["pat"]["chat"] == self.chat_id)
        if self.cfg["target"]["trigger"].get("require_mention_metadata", True):
            return self.store.self_wxid in (msg.get("mentioned_users") or [])
        return is_triggered(self.cfg, msg.get("text") or "")

    def _accept(self, msg, now):
        return self._admission_reason(msg, now) == "accepted"

    def _admission_reason(self, msg, now):
        sent = message_time(msg)
        max_age = float(self.cfg["target"].get("max_message_age_seconds", 90))
        if not msg.get("sender"):
            return "unknown_sender"
        if msg["sender"] == self.store.self_wxid:
            return "own_message"
        if msg.get("type") != 1 and not is_pat(msg):
            return "unsupported_type"
        if not (msg.get("text") or "").strip():
            return "empty_text"
        if sent is None:
            return "invalid_time"
        if sent > now:
            return "future_time"
        if max_age > 0 and now - sent > max_age:
            return "stale_at_intake"
        if not self._triggered(msg):
            if is_pat(msg):
                return "pat_not_for_bot" if msg["pat"]["target"] != self.store.self_wxid else "pat_disabled_or_wrong_chat"
            return "missing_mention_metadata" if msg.get("mentioned_users") is None else "not_mentioned"
        return "accepted"

    def _intake_metadata(self, msg, now):
        reference = msg.get("reference") or {}
        return dict(local_id=msg["local_id"], type=msg.get("type"), raw_type=msg.get("raw_type", msg.get("type")),
                    kind=msg.get("message_kind", "text"), received_at=now, sent_at=message_time(msg),
                    reason=self._admission_reason(msg, now), real_mention=self.store.self_wxid in (msg.get("mentioned_users") or []),
                    pat_for_bot=is_pat(msg) and msg["pat"]["target"] == self.store.self_wxid,
                    text_has_at="@" in (msg.get("text") or ""), chars=len(msg.get("text") or ""),
                    reference_kind=reference.get("kind"), reference_available=reference.get("available"),
                    reference_chars=len(reference.get("text") or ""))

    def _poll_once(self):
        while True:
            rows = self.store.messages_after(self.chat_id, self.last_local_id, limit=100)
            now, old_cursor = time.time(), self.last_local_id
            events = self.journal.intake(rows, lambda m: self._accept(m, now), now,
                                         float(self.cfg["queue"]["debounce_seconds"]),
                                         describe=lambda m: self._intake_metadata(m, now))
            for event in events:
                if event["real_mention"] or event["text_has_at"] or event["kind"] == "pat":
                    log.info("接收判定 metadata=%s", json.dumps(event, ensure_ascii=False))
            if len(rows) < 100:
                return
            if self.last_local_id <= old_cursor:
                raise StoreError("消息分页游标未前进")

    def _generation_step(self):
        for task_id, (version, job) in list(self.generators.items()):
            if self.journal.task(task_id)["version"] != version:
                job.close()
                del self.generators[task_id]
                log.info("废弃生成 task=%s version=%s", task_id, version)
                continue
            result = job.poll()
            if result is None:
                continue
            if result.get("event") == "search_started":
                settings = self.cfg.get("search", {})
                if settings.get("feedback_enabled", True):
                    phrases = settings.get("feedback_phrases") or ["我上网搜搜，核实一下。"]
                    notice = format_reply(phrases[(task_id-1) % len(phrases)], self.cfg)
                    if self.journal.request_notice(task_id, version, notice):
                        log.info("搜索提示待发送 task=%s version=%s", task_id, version)
                continue
            job.close()
            del self.generators[task_id]
            reply = result.get("reply") or ""
            error = "generation:" + result.get("error", "EmptyReply") if not result["ok"] or not reply else ""
            chunks = reply_chunks(reply, int(self.cfg["target"].get("max_reply_chars", 1800))) if not error else []
            if self.journal.generated(task_id, version, chunks, error, vision=result.get("vision"), search=result.get("search")):
                log.info("生成结束 task=%s version=%s chunks=%s images=%s error=%s", task_id, version,
                         len(chunks), (result.get("vision") or {}).get("images", 0), error or "none")
        for task in sorted(self.journal.tasks(), key=lambda t: (t["deadline"], t["id"])):
            if len(self.generators) >= int(self.cfg["queue"]["generation_concurrency"]):
                break
            if task["generation"] != "pending":
                continue
            if self.memory_job and len(self.generators)+1 >= min(2, int(self.cfg["queue"]["generation_concurrency"])):
                self._pause_memory()
            task_id, version = task["id"], task["version"]
            try:
                trigger = task["triggers"][-1]
                start, end = context_bounds(self.cfg, trigger)
                context = self.store.messages_in_window(self.chat_id, start, end)
                memory = None
                if self.memory:
                    try:
                        labels = member_labels(select_context(self.cfg, context, trigger), task["triggers"], self.store.self_wxid)
                        memory = dict(labels=labels, text=self.memory.prompt(labels, trigger["sender"], time.time(),
                                      int(self.cfg["memory"]["prompt_max_chars"]),
                                      use_avatar=self.cfg["memory"].get("avatar", {}).get("enabled", False),
                                      dialogue='\n'.join(m.get('text', '') for m in [*context, trigger]),
                                      use_learning=self.cfg['memory']['learning']['enabled']))
                    except Exception as exc:
                        self.memory_error = type(exc).__name__
                        log.warning("跳过记忆背景 category=%s", self.memory_error)
                prompt = build_prompt(self.cfg, context, trigger, self.chat_title, task["triggers"], memory=memory)
                echo = "[echo] " + "\n".join(m["text"] for m in task["triggers"]) if self.echo else None
                timeout = 3 * float(self.cfg["deepseek"].get("timeout_seconds", 60)) + 15
                if self.cfg.get("search", {}).get("enabled", True) and not self.echo:
                    timeout = float(self.cfg.get("search", {}).get("total_timeout_seconds", 120)) + 15
                images = []
                if self.cfg.get("vision", {}).get("enabled", True) and not self.echo:
                    for index, question in enumerate(task["triggers"]):
                        reference, depth = question.get("reference"), 0
                        while isinstance(reference, dict):
                            if reference.get("kind") == "image":
                                images.append(dict(trigger_index=index, depth=depth,
                                                   **self.store.image_binding(self.chat_id, reference)))
                            reference, depth = reference.get("reference"), depth+1
                payload = (self.cfg, prompt, echo)
                if images:
                    payload += (dict(context=context, triggers=task["triggers"], chat_title=self.chat_title, images=images,
                                     memory=memory),)
                job = self.job_factory("generate", payload, timeout=timeout)
                with self.journal.db:
                    task["generation"] = "running"
                    task["memory_used"] = dict(revision=self.memory.data["revision"] if self.memory and memory else None,
                                               chars=len((memory or {}).get("text", "")))
                    self.journal.put(task)
                self.generators[task_id] = (version, job)
                log.info("开始生成 task=%s version=%s questions=%s references=%s", task_id, version,
                         len(task["triggers"]), sum(isinstance(m.get("reference"), dict) for m in task["triggers"]))
            except Exception as exc:
                self.journal.generated(task_id, version, [], "generation:" + type(exc).__name__)
                log.error("生成未启动 task=%s category=%s", task_id, type(exc).__name__)

    def _ui_result_step(self):
        if self.ui_job is None:
            return
        task_id, part, job = self.ui_job
        result = job.poll()
        if result is None:
            return
        job.close()
        self.ui_job = None
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

    def _confirm_step(self):
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
                    self.journal.acknowledge(task["id"], msg["local_id"], time.time(), memory=self.memory is not None,
                                             proactive_config=self.cfg.get('proactive'))
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

    def _send_step(self):
        if self.ui_job is not None:
            return
        task = self.journal.head()
        if not task or task["phase"] != "queued" or task["intent"] or task["blocked_reason"]:
            return
        notice = (task.get("notice") or {}).get("state") == "pending"
        if not notice and task["generation"] != "ready":
            return
        now = time.time()
        if now < task.get("retry_at", 0):
            return
        if now - self.journal.get("last_success", 0) < float(self.cfg["target"].get("cooldown_seconds", 8)):
            return
        if self.proactive and not self.proactive.validate(task, now):
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
            job = self.job_factory("send", (self.cfg, self.chat_title, self.journal.outgoing_text(current)), timeout=30)
            self.ui_job = (task["id"], current["intent"]["part"], job)
            log.info("发送意图 task=%s version=%s kind=%s part=%s baseline=%s", task["id"], task["version"],
                     current["intent"]["kind"], current["intent"]["part"], baseline)
        except Exception as exc:
            with self.journal.db:
                current = self.journal.task(task["id"])
                current["intent"]["deadline"] = now + timeout
                current["intent"]["ui"] = {"error": type(exc).__name__}
                self.journal.put(current)

    def _pause_memory(self):
        if self.memory_job:
            record, _, worker = self.memory_job
            worker.close()
            self.memory_job = None
            with self.journal.db:
                record["state"] = "pending"
                self.journal.put_memory(record)

    def _memory_step(self):
        if not self.memory:
            return
        now = time.time()
        record = None
        try:
            if self.memory_job:
                record, value, worker = self.memory_job
                result = worker.poll()
                if result is None:
                    return
                worker.close()
                self.memory_job = None
                if not result.get("ok"):
                    raise ValueError("memory_worker_failed")
                self.memory.apply(result["proposal"], value, now)
                self.journal.finish_memory(record["id"], now)
                log.info("记忆更新 task=%s revision=%s", record["id"], self.memory.data["revision"])
            enqueue_if_active(self, now)
            jobs = self.journal.memory_jobs()
            if now-self.memory_pruned_at >= 60:
                self.memory.prune(now)
                self.memory_pruned_at = now
            occupied = len(self.generators)+int(bool(self.proactive and self.proactive.job))
            if not jobs or occupied >= min(2, int(self.cfg["queue"]["generation_concurrency"])):
                return
            record = jobs[0]
            if max(m["local_id"] for m in record["triggers"]) <= self.memory.data["watermark"]:
                self.journal.finish_memory(record["id"], self.journal.get("memory_last_update", 0))
                return
            if now < record["retry_at"] or now-self.journal.get("memory_last_update", 0) < self.cfg["memory"]["min_interval_seconds"]:
                return
            if any(t["generation"] == "pending" for t in self.journal.tasks()):
                return
            pointers = record["triggers"]
            start, end = record.get('window') or context_bounds(self.cfg, pointers[-1])
            context = self.store.messages_in_window(self.chat_id, start, end)
            ids = {m["local_id"] for m in pointers}
            triggers = [m for m in context if m["local_id"] in ids]
            if len(triggers) != len(ids):
                raise ValueError("memory_source_unavailable")
            value = snapshot(self.cfg, context, triggers, self.journal.get("identity"), self.memory, now, window=record.get('window'))
            if record.get('origin') == 'activity':
                settings = self.cfg['memory']['learning']
                fresh = eligible_messages([m for m in context if m['local_id'] <= max(ids)], self.store.self_wxid, end, settings,
                    max(self.memory.data['watermark'], self.memory.data['learning']['started_after_id'],
                        self.journal.get('passive_learning', {}).get('started_after_id', 0)),
                    self.memory.data.get('processed_ids', []))
                if (not settings['enabled'] or not settings['passive_enabled'] or len(fresh) < settings['min_new_messages']
                        or len({m['sender'] for m in fresh}) < settings['min_senders']):
                    self.journal.finish_memory(record['id'], self.journal.get('memory_last_update', 0))
                    return
            if not value["messages"] and not value.get("interactions"):
                # 遗忘水位或已处理的重叠窗口，不额外调用模型。
                self.journal.finish_memory(record["id"], self.journal.get("memory_last_update", 0))
                return
            avatars = self.cfg["memory"].get("avatar", {})
            if avatars.get("enabled"):
                value["avatars"] = {}
                new_labels = {m["member"] for m in [*value["messages"], *value.get("interactions", [])]}
                for uid in sorted(value["members"], key=lambda uid: int(value["members"][uid]["label"][1:])):
                    if uid not in self.memory.data["members"] and value["members"][uid]["label"] not in new_labels:
                        continue  # 已遗忘且只有旧发言的成员，不从旧窗口再导入头像。
                    initial = self.memory.data["members"].get(uid, {}).get("initial_impression", {})
                    if initial.get("state") in ("ready", "forgotten", "expired"):
                        continue
                    if now-initial.get("checked_at", 0) < avatars["retry_interval_seconds"]:
                        continue
                    try:
                        item = self.store.avatar(uid, max_bytes=avatars["max_bytes"])
                        if not isinstance(item, dict):
                            raise ValueError("avatar_invalid_binding")
                    except Exception:
                        item = dict(available=False, error="avatar_unavailable")
                    value["avatars"][uid] = item
                    if len(value["avatars"]) >= avatars["max_per_batch"]:
                        break
            worker = self.job_factory("memory", (self.cfg, value),
                                      timeout=self.cfg["memory"]["timeout_seconds"]+2)
            with self.journal.db:
                record["state"] = "running"
                self.journal.put_memory(record)
            self.memory_job = (record, value, worker)
        except Exception as exc:
            # 永不输出异常消息、模型输出或记忆正文。
            if self.memory_job:
                self.memory_job[2].close()
                self.memory_job = None
            with self.journal.db:
                if record:
                    record.update(state="pending", attempts=record["attempts"]+1,
                                  retry_at=now+max(self.cfg['memory']['learning']['min_interval_seconds'] if record.get('origin') == 'activity' else 0,
                                                   min(300, 60*(record["attempts"]+1))))
                    self.journal.put_memory(record)
                import re
                code = str(exc) if re.fullmatch(r"memory_[a-z_]+", str(exc)) else type(exc).__name__
                self.journal.set("memory_error", code)
            log.warning("记忆延后 category=%s", code)

    def tick(self):
        self._poll_once()
        if self.proactive:
            self.proactive.priority(time.time())
        self.journal.seal(time.time())
        self._generation_step()
        self._ui_result_step()
        self._confirm_step()
        # 工作进程/数据库操作期间有新消息时，发送前必须再补读并校验版本。
        self._poll_once()
        if self.proactive:
            self.proactive.priority(time.time())
        self.journal.seal(time.time())
        self._generation_step()
        if self.proactive:
            self.proactive.step(time.time())
        self._send_step()
        self._memory_step()
        self.save_status()

    def save_status(self, *, stopped=False):
        status, head = self.journal.status(), self.journal.head()
        if head and not head["intent"] and not head["blocked_reason"]:
            if head["phase"] == "queued" and (head.get("notice") or {}).get("state") == "pending":
                status["phase"] = "search_notice_pending"
            elif head["phase"] == "open":
                status["phase"] = "debouncing"
            elif head["generation"] != "ready":
                status["phase"] = "generating"
            elif time.time() - status["last_success"] < float(self.cfg["target"].get("cooldown_seconds", 8)):
                status["phase"] = "cooldown"
            else:
                status["phase"] = "ready"
        status.update(pid=os.getpid(), updated_at=time.time(), started_at=self.started_at,
                      dry_run=bool(self.cfg["ui"].get("dry_run")), echo=self.echo,
                      generation_workers=len(self.generators), context_window_seconds=self.cfg["target"]["context_window_seconds"],
                      require_mention_metadata=True,
                      pat_enabled=self.cfg["target"]["trigger"].get("pat_enabled", True))
        status["memory"] = dict(enabled=self.memory is not None, running=self.memory_job is not None,
                                error=self.memory_error, **(self.memory.status(time.time()) if self.memory else {}))
        status['reply_style'] = {key:self.cfg.get('reply_style', {}).get(key, default)
            for key, default in (('wechat_emoticons_enabled', False), ('omit_trailing_period', True))}
        if stopped:
            status["phase"] = "stopped"
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.status_path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(status, stream, ensure_ascii=False)
        os.replace(temp, self.status_path)

    def close(self):
        if self.proactive:
            self.proactive.cancel('stopped')
        self._pause_memory()
        for _, job in self.generators.values():
            job.close()
        if self.ui_job:
            self.ui_job[2].close()
        try:
            self.save_status(stopped=True)
        finally:
            self.journal.close()

    def run(self, once=False):
        try:
            while True:
                self.tick()
                if once:
                    return
                time.sleep(min(float(self.cfg.get("poll_interval_seconds", 2)), 0.5))
        except KeyboardInterrupt:
            log.info("停止监听，未完成任务保留在内存队列")
