"""群实例拥有模型线程通道；主循环独占数据库，UI 作业使用隔离进程。"""
from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time

from wxbridge.ai.context import message_time
from wxbridge.storage.journal import Journal
from wxbridge.wechat.store import StoreError, WeChatStore
from wechat_linux.messages.pats import is_pat
from wxbridge.app.workers import ProcessJob
from wxbridge.app.session_phases import SessionPhases
from wxbridge.app.participation import ParticipationController
from wxbridge.app.delivery import DeliveryController
from wxbridge.app.reply import ReplyController
from wxbridge.app.memory import MemoryController
from wxbridge.wechat.messages import chunk_text
from wxbridge.app.metrics import Metrics
from wxbridge.app.build_info import runtime_identity

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


class GroupSession(SessionPhases):
    def __init__(self, cfg, echo=False, *, store=None, account=None, instance_id=None, start_gateway=True):
        from wxbridge.instances import CHAT_ID, INSTANCE_ID
        if instance_id is not None and (not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id)):
            raise ValueError('invalid_instance_id')
        from wxbridge.native.protocol import settings as native_settings
        native_options = native_settings(cfg)
        self.cfg, self.echo = cfg, echo
        self.account, self.instance_id = account, instance_id
        self.quiescing = False
        from wxbridge.app.logging_context import InstanceLog
        self.log = InstanceLog(log, dict(instance=instance_id)) if instance_id else log
        self.store = store if store is not None else WeChatStore(cfg["db_dir"], cfg["keys_file"], cfg["decrypted_dir"])
        self.chat_id, self.chat_title = self.store.resolve_chat(cfg["target"]["chat"])
        if not CHAT_ID.fullmatch(self.chat_id) or not self.store.self_wxid:
            raise StoreError("必须确认当前微信账号及目标群聊或用户私聊")
        if cfg['ui'].get('backend')=='native_service' and (not native_options['enabled'] or self.chat_id not in native_options['allowed_chat_ids']):
            raise ValueError('native_service requires enabled native_api and an allowed target chat')
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
        if instance_id is not None:
            saved = self.journal.get('instance_id')
            if saved is not None and saved != instance_id:
                self.journal.close()
                raise ValueError('queue_instance_identity_mismatch')
            with self.journal.db:
                self.journal.set('instance_id', instance_id)
        self.journal.recover(time.time())
        self.delivery = DeliveryController(cfg, self.journal, self.store, self.chat_id, self.chat_title, logger=self.log)
        self.replies = ReplyController(self)
        self.ui_job = None
        self.memory = MemoryController(self)
        self.started_at = time.time()
        from wxbridge.app.model_runtime import GroupModels
        self.models = GroupModels(cfg)
        from wxbridge.app.image_generation import ImageGenerationController
        self.image_generation = ImageGenerationController(self)
        self.job_factory = self._new_job
        self.status_path = Path(cfg["state_file"]).parent / "live-status.json"
        try:
            self.participation = ParticipationController(self)
        except BaseException:
            self.models.close()
            self.journal.close()
            raise
        self.maintenance_at = 0
        self.metrics, self.build = Metrics(), runtime_identity(cfg)
        self.status_signature, self.status_written_at = None, 0
        self.status_write_error = ''
        self.next_poll_at = 0
        self.profile = None
        if cfg['profile']['enabled'] and not cfg['ui'].get('dry_run') and not echo:
            from wxbridge.app.group_profile import ProfileController
            try:
                self.profile = ProfileController(self)
            except Exception:
                self.models.close()
                self.journal.close()
                raise
        self.native_api = None
        if start_gateway and native_options['enabled'] and not cfg['ui'].get('dry_run'):
            from wxbridge.native.server import NativeServer
            from wxbridge.app.native_gateway import BotNativeService
            self.native_api = NativeServer(native_options, BotNativeService(self, native_options))

    _chunk = staticmethod(chunk_text)

    @property
    def last_local_id(self):
        return self.journal.get("cursor", 0)

    def _accept(self, msg, now):
        return self._admission_reason(msg, now) == "accepted"

    def _admission_reason(self, msg, now):
        sent = message_time(msg)
        max_age = float(self.cfg["target"].get("max_message_age_seconds", 90))
        if not msg.get("sender"):
            return "unknown_sender"
        if msg["sender"] == self.store.self_wxid:
            return "own_message"
        if not self.chat_id.endswith('@chatroom') and msg['sender'] != self.chat_id:
            return 'private_sender_mismatch'
        if not self.cfg['participation']['enabled']:
            return 'participation_disabled'
        if msg.get("type") != 1 and not is_pat(msg):
            return "unsupported_type"
        if not is_pat(msg) and not (msg.get("text") or "").strip():
            return "empty_text"
        if sent is None:
            return "invalid_time"
        if sent > now:
            return "future_time"
        deferred = self.journal.get('intake_deferred') or {}
        previously_seen = deferred.get('local_id') == msg.get('local_id')
        if max_age > 0 and now - sent > max_age and not previously_seen:
            return "stale_at_intake"
        if is_pat(msg):
            if not self.cfg['participation']['pat_enabled']:
                return 'pat_disabled'
            if msg['pat']['chat'] != self.chat_id or msg['pat']['target'] != self.store.self_wxid:
                return 'pat_not_for_bot'
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
            if self.profile:
                self.profile.observe(rows)
            now, old_cursor = time.time(), self.last_local_id
            events = self.journal.intake_participation(rows, lambda m: self._accept(m, now), now,
                ttl_seconds=float(self.cfg['target']['max_message_age_seconds']), limits=self.cfg['queue'],
                describe=lambda m: self._intake_metadata(m, now))
            for event in events:
                if event["real_mention"] or event["text_has_at"] or event["kind"] == "pat":
                    self.log.info("接收判定 metadata=%s", json.dumps(event, ensure_ascii=False))
            if len(rows) < 100:
                return
            if self.journal.get('backpressure'):
                return
            if self.last_local_id <= old_cursor:
                raise StoreError("消息分页游标未前进")

    def _new_job(self, kind, payload, *, timeout):
        if kind in ('generate', 'memory', 'participation', 'group_profile'):
            return self.models.submit(kind, payload, timeout=timeout)
        return ProcessJob(kind, payload, timeout=timeout)

    def submit_images(self, request, *, deadline=None):
        return self.models.submit_images(request, deadline=deadline)

    def profile_snapshot(self, rows):
        return self.profile.snapshot(rows) if self.profile else None

    def _poll_messages(self, *, force=False):
        if not force and time.monotonic() < self.next_poll_at:
            return False
        with self.metrics.measure('pre_send_intake' if force else 'intake'):
            self._poll_once()
        self.next_poll_at = time.monotonic() + float(self.cfg.get('poll_interval_seconds', 2))
        return True

    def _refresh_before_send(self):
        if self.account is not None:
            self.account.refresh_before_send()
            return
        self._poll_messages(force=True)
        self.participation.priority(time.time())

    @property
    def ui_job(self):
        return self.delivery.job

    @ui_job.setter
    def ui_job(self, job):
        self.delivery.job = job

    def _ui_result_step(self):
        self.delivery.poll_result()

    def _confirm_step(self):
        self.delivery.confirm(memory=self.memory.store is not None)

    def _send_step(self):
        account = getattr(self, 'account', None)
        if account is not None and not account.can_send(self):
            return
        self.delivery.send(self.job_factory, self._validate_delivery,
                           refresh=self._refresh_before_send)

    def _validate_delivery(self, task, now):
        if self.account is not None and not self.account.can_send(self):
            return False
        return (self.journal.get('identity') == dict(account=self.store.self_wxid, chat=self.chat_id)
                and (task.get('next_chunk', 0) > 0 or now < task.get('reply_expires_at', float('inf'))))

    def tick(self):
        self.run_phases(self.native_api)

    def maintain(self):
        if time.time()-self.maintenance_at >= 60:
            protected = [*self.replies.jobs, *([self.ui_job[0]] if self.ui_job else [])]
            protected.extend(t['parent_task_id'] for t in self.journal.image_tasks())
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
                      model_channels=self.models.status(), generation_workers=len(self.replies.jobs),
                      context_policy={key:self.cfg['target'][key] for key in ('context_recent_messages',
                          'context_max_age_seconds','context_bot_messages','context_max_chars','context_budget_strategy')},
                      context=self.journal.get('context', {}),
                      pat_enabled=self.cfg['participation']['pat_enabled'])
        status.update(build=self.build, metrics=self.metrics.snapshot())
        status['configured_backend'] = self.cfg['ui']['backend']
        status['alerts'] = [name for name, present in (
            ('send_blocked', bool(status.get('blocked_reason'))),
            ('queue_backpressure', bool(status.get('backpressure'))),
            ('memory_quarantined', bool(status.get('memory_quarantined')))) if present]
        status['memory'] = self.memory.status()
        status['profile'] = self.profile.status() if self.profile else dict(enabled=False)
        status['image_generation'] = dict(enabled=self.cfg['image_generation']['enabled'],
            running=len(self.image_generation.jobs), last=status.get('last_image_generation'))
        status['reply_style'] = {key:self.cfg.get('reply_style', {}).get(key, default)
            for key, default in (('wechat_emoticons_enabled', False), ('omit_trailing_period', True))}
        if self.instance_id is not None:
            status['instance_id'] = self.instance_id
        if stopped:
            status["phase"] = "stopped"
        signature = json.dumps({k: v for k, v in status.items() if k not in ('updated_at', 'metrics')}, sort_keys=True)
        clock = time.monotonic()
        if not stopped and signature == self.status_signature and clock-self.status_written_at < 2:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.status_path.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as stream:
                os.chmod(temp, 0o600)
                json.dump(status, stream, ensure_ascii=False)
            os.replace(temp, self.status_path)
        except OSError as exc:
            # Only the diagnostic snapshot is optional. Queue/reader/identity
            # failures above retain their original fail-closed behavior.
            code = type(exc).__name__
            if code != self.status_write_error:
                self.log.warning('状态快照写入失败 category=%s', code)
            self.status_write_error = code
            return False
        self.status_write_error = ''
        self.status_signature, self.status_written_at = signature, clock
        return True

    def begin_stop(self):
        """Cancel model results without blocking the other account sessions."""
        self.quiescing = True
        self.image_generation.close()
        self.models.begin_close()

    def drain_stop(self):
        # Keep resolving already-submitted actions. Never submit another message.
        self._ui_result_step()
        self._confirm_step()
        return self.ui_job is None and self.models.stopped()

    def close(self):
        # A failed child cleanup must stop the runtime, but must not prevent
        # attempts to release the remaining children, gateway and database.
        with ExitStack() as stack:
            stack.callback(self.journal.close)
            if hasattr(self, "models"):
                stack.callback(self.models.close)
            if hasattr(self, 'image_generation'):
                stack.callback(self.image_generation.close)
            stack.callback(self.save_status, stopped=True)
            if self.ui_job:
                stack.callback(self.ui_job[2].close)
            stack.callback(self.replies.close)
            stack.callback(self.memory.close)
            if self.profile:
                stack.callback(self.profile.close)
            stack.callback(self.participation.cancel, 'stopped')
            if self.native_api:
                stack.callback(self.native_api.close)

    def run(self, once=False):
        try:
            while True:
                self.tick()
                if once:
                    return
                # 队列、工作进程与发送确认每秒调度；消息查询另由 next_poll_at 限流。
                time.sleep(min(float(self.cfg.get("poll_interval_seconds", 2)), 1.0))
        except KeyboardInterrupt:
            self.log.info("停止监听，未完成任务保留在内存队列")
