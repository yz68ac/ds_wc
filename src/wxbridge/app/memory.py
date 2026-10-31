"""Owner-loop memory updates and recovery; model threads never mutate memory."""
from pathlib import Path
import time

from wxbridge.ai.context import memory_bounds
from wxbridge.ai.memory import snapshot
from wxbridge.ai.roles import model_settings
from wxbridge.app.learning import enqueue_if_active, eligible_messages
from wxbridge.app.workers import WorkerLease, WorkerCleanupError
from wxbridge.config import PROJECT_ROOT
from wxbridge.storage.person_memory import MemoryStore


class MemoryController:
    def __init__(self, session):
        self.bot = session
        self.store, self.job, self.error, self.pruned_at = None, None, '', 0
        cfg = session.cfg
        if cfg.get('memory', {}).get('enabled') and not cfg['ui'].get('dry_run') and not session.echo:
            try:
                directory = Path(cfg['memory']['directory'])
                if not directory.is_absolute():
                    directory = PROJECT_ROOT/directory
                self.store = MemoryStore(directory, session.journal.get('identity'))
                self.store.enable_learning(session.last_local_id, time.time(), cfg['memory']['learning']['enabled'])
                settings = cfg['memory']['learning']
                enabled = settings['enabled'] and settings['passive_enabled']
                state = session.journal.get('passive_learning')
                with session.journal.db:
                    if state is None or (enabled and not state.get('enabled', False)):
                        state = dict(started_after_id=session.last_local_id, checked_id=session.last_local_id,
                                     checked_at=0, enqueued_at=0)
                    state['enabled'] = enabled
                    session.journal.set('passive_learning', state)
            except Exception as exc:
                self.error = type(exc).__name__
                session.log.error('记忆初始化失败 category=%s', self.error)

    def status(self):
        return dict(enabled=self.store is not None, running=self.job is not None, error=self.error,
                    **(self.store.status(time.time()) if self.store else {}))

    def close(self):
        if self.job:
            record, _, worker = self.job
            worker.close()
            self.job = None
            with self.bot.journal.db:
                record["state"] = "pending"
                self.bot.journal.put_memory(record)

    def step(self, *, collect_only=False):
        if not self.store:
            return
        now = time.time()
        record = None
        try:
            if self.job:
                record, value, worker = self.job
                result = worker.poll()
                if result is None:
                    return
                worker.close()
                self.job = None
                self.bot.metrics.record_models(result.get('model_metrics'))
                if not result.get("ok"):
                    from wxbridge.app.task_policy import memory_failure_code
                    raise ValueError(memory_failure_code(result))
                self.store.apply(result["proposal"], value, now)
                self.bot.journal.finish_memory(record["id"], now)
                if record.get('learning_suppressed'):
                    with self.bot.journal.db:
                        self.bot.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                            reason=record['recovery_reason'], stage='completed'))
                self.bot.log.info("记忆更新 task=%s revision=%s", record["id"], self.store.data["revision"])
            if collect_only:
                return
            enqueue_if_active(self.bot, now)
            jobs = self.bot.journal.memory_jobs()
            if now-self.pruned_at >= 60:
                self.store.prune(now)
                self.pruned_at = now
            if not jobs or self.job:
                return
            record = jobs[0]
            if max(m["local_id"] for m in record["triggers"]) <= self.store.data["watermark"]:
                self.bot.journal.finish_memory(record["id"], self.bot.journal.get("memory_last_update", 0))
                return
            previous_error = record.get('last_error', self.bot.journal.get('memory_error', ''))
            source_errors = ('memory_unknown_source', 'memory_unknown_member')
            if (record['attempts'] >= 3 and isinstance(previous_error, str)
                    and (previous_error.startswith('memory_learning_') or previous_error in source_errors)
                    and not record.get('learning_suppressed')):
                # 新请求明确收窄能力；旧失败结果不写入，来源校验保持不变。
                record.update(learning_suppressed=True, recovery_reason=previous_error, retry_at=now)
                with self.bot.journal.db:
                    self.bot.journal.put_memory(record)
                    self.bot.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                        reason=previous_error, stage='pending'))
            if now < record["retry_at"] or now-self.bot.journal.get("memory_last_update", 0) < self.bot.cfg["memory"]["min_interval_seconds"]:
                return
            if any(t["generation"] == "pending" for t in self.bot.journal.tasks()):
                return
            pointers = record["triggers"]
            start, end = record.get('window') or memory_bounds(self.bot.cfg, pointers[-1])
            context = self.bot.store.messages_in_window(self.bot.chat_id, start, end)
            ids = {m["local_id"] for m in pointers}
            triggers = [m for m in context if m["local_id"] in ids]
            if len(triggers) != len(ids):
                raise ValueError("memory_source_unavailable")
            value = snapshot(self.bot.cfg, context, triggers, self.bot.journal.get("identity"), self.store, now,
                             window=record.get('window'), profile=self.bot.profile_snapshot([*context, *triggers]))
            if record.get('origin') == 'activity':
                settings = self.bot.cfg['memory']['learning']
                fresh = eligible_messages([m for m in context if m['local_id'] <= max(ids)], self.bot.store.self_wxid, end, settings,
                    max(self.store.data['watermark'], self.store.data['learning']['started_after_id'],
                        self.bot.journal.get('passive_learning', {}).get('started_after_id', 0)),
                    self.store.data.get('processed_ids', []))
                if (not settings['enabled'] or not settings['passive_enabled'] or len(fresh) < settings['min_new_messages']
                        or len({m['sender'] for m in fresh}) < settings['min_senders']):
                    self.bot.journal.finish_memory(record['id'], self.bot.journal.get('memory_last_update', 0))
                    return
            if not value["messages"] and not value.get("interactions"):
                # 遗忘水位或已处理的重叠窗口，不额外调用模型。
                self.bot.journal.finish_memory(record["id"], self.bot.journal.get("memory_last_update", 0))
                return
            if record.get('learning_suppressed'):
                value.update(learning_enabled=False, learning_source_ids=[], learning_existing=[],
                             recovery=dict(section='expression_learning', reason=record['recovery_reason']))
                if record['recovery_reason'] in source_errors:
                    value['dialogue'] = []  # 本批只用新本人发言；不从旧对白提取证据。
            avatars = self.bot.cfg["memory"].get("avatar", {})
            if avatars.get("enabled"):
                value["avatars"] = {}
                new_labels = {m["member"] for m in [*value["messages"], *value.get("interactions", [])]}
                for uid in sorted(value["members"]):
                    if uid not in self.store.data["members"] and value["members"][uid]["label"] not in new_labels:
                        continue  # 已遗忘且只有旧发言的成员，不从旧窗口再导入头像。
                    initial = self.store.data["members"].get(uid, {}).get("initial_impression", {})
                    if initial.get("state") in ("ready", "forgotten", "expired"):
                        continue
                    if now-initial.get("checked_at", 0) < avatars["retry_interval_seconds"]:
                        continue
                    try:
                        item = self.bot.store.avatar(uid, max_bytes=avatars["max_bytes"])
                        if not isinstance(item, dict):
                            raise ValueError("avatar_invalid_binding")
                    except Exception:
                        item = dict(available=False, error="avatar_unavailable")
                    value["avatars"][uid] = item
                    if len(value["avatars"]) >= avatars["max_per_batch"]:
                        break
            with WorkerLease(self.bot.job_factory("memory", (self.bot.cfg, value),
                                              timeout=model_settings(self.bot.cfg, 'memory')['timeout_seconds']+2)) as lease:
                with self.bot.journal.db:
                    record["state"] = "running"
                    self.bot.journal.put_memory(record)
                    if record.get('learning_suppressed'):
                        self.bot.journal.set('memory_recovery', dict(task_id=record['id'], section='expression_learning',
                            reason=record['recovery_reason'], stage='running'))
                self.job = (record, value, lease.job)
                lease.transfer()
        except WorkerCleanupError:
            raise
        except Exception as exc:
            # 永不输出异常消息、模型输出或记忆正文。
            if self.job:
                self.job[2].close()
                self.job = None
            with self.bot.journal.db:
                import re
                code = str(exc) if re.fullmatch(r"memory_[a-z0-9_]{1,64}", str(exc)) else type(exc).__name__
                if record:
                    record.update(state="pending", attempts=record["attempts"]+1, last_error=code,
                                  retry_at=now+max(self.bot.cfg['memory']['learning']['min_interval_seconds'] if record.get('origin') == 'activity' else 0,
                                                   min(300, 60*(record["attempts"]+1))))
                    terminal = code in ('memory_missing_api_key', 'memory_http_401', 'memory_http_403')
                    maximum = 8 if record.get('learning_suppressed') else 5
                    if terminal or record['attempts'] >= maximum:
                        record.update(state='quarantined', quarantined_at=now)
                    self.bot.journal.put_memory(record)
                self.bot.journal.set("memory_error", code)
            self.bot.log.warning("记忆延后 category=%s", code)
