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
from wxbridge.app.task_policy import GenerationFailure
from wxbridge.app.delivery import DeliveryController
from wxbridge.app.metrics import Metrics
from wxbridge.app.build_info import runtime_identity
from wxbridge.ai.roles import model_settings
from wxbridge.storage.memory_selection import memory_dialogue

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
        from wxbridge.native.protocol import settings as native_settings
        native_options = native_settings(cfg)
        self.cfg, self.echo = cfg, echo
        self.store = WeChatStore(cfg["db_dir"], cfg["keys_file"], cfg["decrypted_dir"])
        self.chat_id, self.chat_title = self.store.resolve_chat(cfg["target"]["chat"])
        if not self.chat_id.endswith("@chatroom") or not self.store.self_wxid:
            raise StoreError("必须确认当前微信账号及目标普通群聊")
        if cfg['ui'].get('backend')=='native_service' and (not native_options['enabled'] or self.chat_id not in native_options['allowed_chat_ids']):
            raise ValueError('native_service requires enabled native_api and an allowed target group')
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
        self.delivery = DeliveryController(cfg, self.journal, self.store, self.chat_id, self.chat_title)
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
        self.maintenance_at = 0
        self.metrics, self.build = Metrics(), runtime_identity(cfg)
        self.status_signature, self.status_written_at = None, 0
        self.next_poll_at = 0
        self.native_api = None
        if native_options['enabled'] and not cfg['ui'].get('dry_run'):
            from wxbridge.native.api import NativeAPI
            self.native_api = NativeAPI(self)

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
        deferred = self.journal.get('intake_deferred') or {}
        previously_seen = deferred.get('local_id') == msg.get('local_id')
        if max_age > 0 and now - sent > max_age and not previously_seen:
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
        for _ in range(int(self.cfg['queue'].get('intake_max_pages', 4))):
            rows = self.store.messages_after(self.chat_id, self.last_local_id, limit=100)
            now, old_cursor = time.time(), self.last_local_id
            events = self.journal.intake(rows, lambda m: self._accept(m, now), now,
                                         float(self.cfg["queue"]["debounce_seconds"]),
                                         describe=lambda m: self._intake_metadata(m, now),
                                         observe=(lambda m, accepted: self.proactive.observe(m, accepted, now))
                                         if self.proactive else None, limits=self.cfg['queue'])
            for event in events:
                if event["real_mention"] or event["text_has_at"] or event["kind"] == "pat":
                    log.info("接收判定 metadata=%s", json.dumps(event, ensure_ascii=False))
            if len(rows) < 100:
                return
            if self.journal.get('backpressure'):
                return
            if self.last_local_id <= old_cursor:
                raise StoreError("消息分页游标未前进")

    def _poll_messages(self, *, force=False):
        if not force and time.monotonic() < self.next_poll_at:
            return False
        with self.metrics.measure('pre_send_intake' if force else 'intake'):
            self._poll_once()
        self.next_poll_at = time.monotonic() + float(self.cfg.get('poll_interval_seconds', 2))
        # 可选合并模式只在补齐新消息后封批，避免较长轮询跨过窗口末尾。
        self.journal.seal(time.time())
        return True

    def _refresh_before_send(self):
        self._poll_messages(force=True)
        if self.proactive:
            self.proactive.priority(time.time())

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
            self.metrics.record_models(result.get('model_metrics'))
            if result.get("event") == "search_started":
                settings = self.cfg.get("search", {})
                if settings.get("feedback_enabled", False):
                    phrases = settings.get("feedback_phrases") or ["我上网搜搜，核实一下。"]
                    notice = format_reply(phrases[(task_id-1) % len(phrases)], self.cfg)
                    if self.journal.request_notice(task_id, version, notice):
                        log.info("搜索提示待发送 task=%s version=%s", task_id, version)
                continue
            job.close()
            del self.generators[task_id]
            reply = result.get("reply") or ""
            error = "generation:" + GenerationFailure.from_result(result).code if not result["ok"] else ""
            chunks = reply_chunks(reply, int(self.cfg["target"].get("max_reply_chars", 1800))) if not error else []
            if error:
                self._generation_failed(task_id, version, result)
            elif self.journal.generated(task_id, version, chunks, vision=result.get("vision"), search=result.get("search")):
                log.info("生成结束 task=%s version=%s chunks=%s images=%s error=%s", task_id, version,
                         len(chunks), (result.get("vision") or {}).get("images", 0), error or "none")
        for task in sorted(self.journal.tasks(), key=lambda t: (t["deadline"], t["id"])):
            head = self.journal.head()
            if head and head.get('blocked_reason'):
                break
            ahead = sorted(self.journal.tasks(), key=lambda t: (t['deadline'], t['id']))
            if task['id'] not in {t['id'] for t in ahead[:self.cfg['queue'].get('generation_lookahead', 4)]}:
                break
            if len(self.generators) >= int(self.cfg["queue"]["generation_concurrency"]):
                break
            if task["generation"] != "pending" or time.time() < task.get('generation_retry_at', 0):
                continue
            if task.get('generation_attempts', 0) > self.cfg['queue']['generation_retries']:
                self._generation_failed(task['id'], task['version'], dict(error='WorkerExited', retryable=False))
                continue
            if self.memory_job and len(self.generators)+1 >= min(2, int(self.cfg["queue"]["generation_concurrency"])):
                self._pause_memory()
            task_id, version = task["id"], task["version"]
            self.journal.start_generation(task_id, version, time.time())
            task = self.journal.task(task_id)
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
                                      dialogue=memory_dialogue(context, task['triggers'], self.store.self_wxid),
                                      question=trigger.get('text', ''),
                                      use_learning=self.cfg['memory']['learning']['enabled']))
                    except Exception as exc:
                        self.memory_error = type(exc).__name__
                        log.warning("跳过记忆背景 category=%s", self.memory_error)
                prompt = build_prompt(self.cfg, context, trigger, self.chat_title, task["triggers"],
                                      memory=memory, account=self.store.self_wxid)
                echo = "[echo] " + "\n".join(m["text"] for m in task["triggers"]) if self.echo else None
                timeout = float(model_settings(self.cfg)['timeout_seconds']) + 15
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
                                     memory=memory, account=self.store.self_wxid),)
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
                self._generation_failed(task_id, version, dict(error=type(exc).__name__))
                log.error("生成未启动 task=%s category=%s", task_id, type(exc).__name__)

    def _generation_failed(self, task_id, version, result):
        failure = GenerationFailure.from_result(result)
        settings = self.cfg['queue']
        self.journal.generation_failure(task_id, version, failure.code, time.time(),
            retryable=failure.retryable, retries=settings['generation_retries'],
            delay=settings['generation_retry_delay_seconds'],
            fallback=format_reply('刚才没接上，等会儿再问我一下。', self.cfg))
        log.warning('生成失败 task=%s version=%s category=%s retryable=%s',
                    task_id, version, failure.code, failure.retryable)

    @property
    def ui_job(self):
        return self.delivery.job

    @ui_job.setter
    def ui_job(self, job):
        self.delivery.job = job

    def _ui_result_step(self):
        self.delivery.poll_result()

    def _confirm_step(self):
        self.delivery.confirm(memory=self.memory is not None)

    def _send_step(self):
        self.delivery.send(self.job_factory, self.proactive.validate if self.proactive else None,
                           refresh=self._refresh_before_send)

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
                self.metrics.record_models(result.get('model_metrics'))
                if not result.get("ok"):
                    from wxbridge.app.task_policy import memory_failure_code
                    raise ValueError(memory_failure_code(result))
                self.memory.apply(result["proposal"], value, now)
                self.journal.finish_memory(record["id"], now)
                if record.get('learning_suppressed'):
                    with self.journal.db:
                        self.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                            reason=record['recovery_reason'], stage='completed'))
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
            previous_error = record.get('last_error', self.journal.get('memory_error', ''))
            source_errors = ('memory_unknown_source', 'memory_unknown_member')
            if (record['attempts'] >= 3 and isinstance(previous_error, str)
                    and (previous_error.startswith('memory_learning_') or previous_error in source_errors)
                    and not record.get('learning_suppressed')):
                # 新请求明确收窄能力；旧失败结果不写入，来源校验保持不变。
                record.update(learning_suppressed=True, recovery_reason=previous_error, retry_at=now)
                with self.journal.db:
                    self.journal.put_memory(record)
                    self.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                        reason=previous_error, stage='pending'))
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
            if record.get('learning_suppressed'):
                value.update(learning_enabled=False, learning_source_ids=[], learning_existing=[],
                             recovery=dict(section='expression_learning', reason=record['recovery_reason']))
                if record['recovery_reason'] in source_errors:
                    value['dialogue'] = []  # 本批只用新本人发言；不从旧对白提取证据。
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
                                      timeout=model_settings(self.cfg, 'memory')['timeout_seconds']+2)
            with self.journal.db:
                record["state"] = "running"
                self.journal.put_memory(record)
                if record.get('learning_suppressed'):
                    self.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                        reason=record['recovery_reason'], stage='running'))
            self.memory_job = (record, value, worker)
        except Exception as exc:
            # 永不输出异常消息、模型输出或记忆正文。
            if self.memory_job:
                self.memory_job[2].close()
                self.memory_job = None
            with self.journal.db:
                import re
                code = str(exc) if re.fullmatch(r"memory_[a-z0-9_]{1,64}", str(exc)) else type(exc).__name__
                if record:
                    record.update(state="pending", attempts=record["attempts"]+1, last_error=code,
                                  retry_at=now+max(self.cfg['memory']['learning']['min_interval_seconds'] if record.get('origin') == 'activity' else 0,
                                                   min(300, 60*(record["attempts"]+1))))
                    terminal = code in ('memory_missing_api_key', 'memory_http_401', 'memory_http_403')
                    maximum = 8 if record.get('learning_suppressed') else 5
                    if terminal or record['attempts'] >= maximum:
                        record.update(state='quarantined', quarantined_at=now)
                    self.journal.put_memory(record)
                self.journal.set("memory_error", code)
            log.warning("记忆延后 category=%s", code)

    def tick(self):
        self._poll_messages()
        if self.native_api:
            self.native_api.tick()
        if self.proactive:
            self.proactive.priority(time.time())
        with self.metrics.measure('generation'):
            self._generation_step()
        with self.metrics.measure('ui_result'):
            self._ui_result_step()
        with self.metrics.measure('confirmation'):
            self._confirm_step()
        if self.proactive:
            with self.metrics.measure('participation'):
                self.proactive.step(time.time())
        with self.metrics.measure('delivery'):
            self._send_step()
        with self.metrics.measure('memory'):
            self._memory_step()
        if time.time()-self.maintenance_at >= 60:
            protected = [*self.generators, *([self.ui_job[0]] if self.ui_job else [])]
            self.journal.prune_completed(self.cfg['queue']['completed_retention'], protected)
            self.maintenance_at = time.time()
        self.save_status()

    def save_status(self, *, stopped=False):
        status, head = self.journal.status(), self.journal.head()
        if head and not head["intent"] and not head["blocked_reason"]:
            if head["phase"] == "queued" and (head.get("notice") or {}).get("state") == "pending":
                status["phase"] = "search_notice_pending"
            elif head["phase"] == "open":
                status["phase"] = "debouncing"
            elif head.get('generation_retry_at', 0) > time.time():
                status['phase'] = 'generation_retry_wait'
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
        status.update(build=self.build, metrics=self.metrics.snapshot())
        status['configured_backend'] = self.cfg['ui']['backend']
        status['alerts'] = [name for name, present in (
            ('send_blocked', bool(status.get('blocked_reason'))),
            ('queue_backpressure', bool(status.get('backpressure'))),
            ('memory_quarantined', bool(status.get('memory_quarantined')))) if present]
        status["memory"] = dict(enabled=self.memory is not None, running=self.memory_job is not None,
                                error=self.memory_error, **(self.memory.status(time.time()) if self.memory else {}))
        status['reply_style'] = {key:self.cfg.get('reply_style', {}).get(key, default)
            for key, default in (('wechat_emoticons_enabled', False), ('omit_trailing_period', True))}
        if stopped:
            status["phase"] = "stopped"
        signature = json.dumps({k: v for k, v in status.items() if k not in ('updated_at', 'metrics')}, sort_keys=True)
        clock = time.monotonic()
        if not stopped and signature == self.status_signature and clock-self.status_written_at < 2:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.status_path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(status, stream, ensure_ascii=False)
        os.replace(temp, self.status_path)
        self.status_signature, self.status_written_at = signature, clock

    def close(self):
        if self.native_api:
            self.native_api.close()
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
                # 队列、工作进程与发送确认每秒调度；消息查询另由 next_poll_at 限流。
                time.sleep(min(float(self.cfg.get("poll_interval_seconds", 2)), 1.0))
        except KeyboardInterrupt:
            log.info("停止监听，未完成任务保留在内存队列")
