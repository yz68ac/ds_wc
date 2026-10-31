"""Owner-loop reply tasks: freeze inputs, submit work and apply validated results."""
from contextlib import ExitStack
import re
import time

from wxbridge.ai.context import read_context, source_anchor
from wxbridge.ai.memory import member_labels
from wxbridge.ai.output import format_reply
from wxbridge.ai.prompts import build_prompt
from wxbridge.ai.roles import model_settings
from wxbridge.app.task_policy import GenerationFailure
from wxbridge.app.workers import WorkerLease, WorkerCleanupError
from wxbridge.storage.memory_selection import memory_dialogue
from wxbridge.wechat.messages import reply_chunks


class ReplyController:
    def __init__(self, session):
        self.bot, self.jobs = session, {}

    def collect(self):
        for task_id, (version, job) in list(self.jobs.items()):
            current = self.bot.journal.task(task_id)
            if self.bot.participation.expire_reply(current, time.time()) or current["version"] != version:
                job.close()
                del self.jobs[task_id]
                self.bot.log.info("废弃生成 task=%s version=%s", task_id, version)
                continue
            result = job.poll()
            if result is None:
                continue
            if result.get("error") == "WorkerCleanupError":
                raise WorkerCleanupError()
            self.bot.metrics.record_models(result.get('model_metrics'))
            if result.get('context_stats'):
                with self.bot.journal.db:
                    self.bot.journal.set('context', {**self.bot.journal.get('context', {}), 'reply':result['context_stats']})
            if result.get("event") == "search_started":
                settings = self.bot.cfg.get("search", {})
                if settings.get("feedback_enabled", False):
                    phrases = settings.get("feedback_phrases") or ["我上网搜搜，核实一下。"]
                    notice = format_reply(phrases[(task_id-1) % len(phrases)], self.bot.cfg)
                    if self.bot.journal.request_notice(task_id, version, notice):
                        self.bot.log.info("搜索提示待发送 task=%s version=%s", task_id, version)
                continue
            job.close()
            del self.jobs[task_id]
            reply = result.get("reply") or ""
            error = "generation:" + GenerationFailure.from_result(result).code if not result["ok"] else ""
            chunks = reply_chunks(reply, int(self.bot.cfg["target"].get("max_reply_chars", 1800))) if not error else []
            if error:
                self._failed(task_id, version, result)
            else:
                try:
                    proposal = self.bot.image_generation.proposal(result.get('image_requests'))
                except Exception as exc:
                    self._failed(task_id, version, dict(error=getattr(exc, 'code', 'invalid_image_tool'), retryable=False))
                    continue
                accepted = self.bot.journal.generated(task_id, version, chunks, vision=result.get('vision'),
                    search=result.get('search'), image_request=proposal, now=time.time())
                if not accepted:
                    continue
                self.bot.log.info("生成结束 task=%s version=%s chunks=%s images=%s error=%s", task_id, version,
                         len(chunks), (result.get("vision") or {}).get("images", 0), error or "none")

    def start(self, *, max_new=None):
        started = 0
        candidates = self.bot.journal.generation_candidates(self.bot.cfg['queue'].get('generation_lookahead', 4))
        for task in candidates:
            if task.get('trigger_kind') == 'participation' and self.bot.participation.expire_reply(task, time.time()):
                continue
            head = self.bot.journal.head()
            if head and head.get('blocked_reason'):
                break
            if task["generation"] != "pending" or time.time() < task.get('generation_retry_at', 0):
                continue
            if (max_new is not None and started >= max_new):
                break
            if task.get('generation_attempts', 0) > self.bot.cfg['queue']['generation_retries']:
                self._failed(task['id'], task['version'], dict(error='WorkerExited', retryable=False))
                continue
            task_id, version = task["id"], task["version"]
            self.bot.journal.start_generation(task_id, version, time.time())
            task = self.bot.journal.task(task_id)
            try:
                trigger = source_anchor(task['triggers'])
                context = read_context(self.bot.cfg,self.bot.store,self.bot.chat_id,task['triggers'],account=self.bot.store.self_wxid)
                memory, memory_view = None, None
                if self.bot.memory.store:
                    try:
                        memory_view = self.bot.memory.store.view()
                        labels = member_labels(context, task["triggers"], self.bot.store.self_wxid)
                        memory = dict(labels=labels, text=memory_view.prompt(labels, trigger["sender"], time.time(),
                                      int(self.bot.cfg["memory"]["prompt_max_chars"]),
                                      use_avatar=self.bot.cfg["memory"].get("avatar", {}).get("enabled", False),
                                      dialogue=memory_dialogue(context, task['triggers'], self.bot.store.self_wxid),
                                      question=trigger.get('text', ''),
                                      use_learning=self.bot.cfg['memory']['learning']['enabled']),
                                      relationship=memory_view.reply_relationship(trigger['sender'], time.time()),
                                      relationship_for=trigger['sender'])
                    except Exception as exc:
                        self.bot.memory.error = type(exc).__name__
                        self.bot.log.warning("跳过记忆背景 category=%s", self.bot.memory.error)
                profile = self.bot.profile_snapshot([*context, *task['triggers']])
                prompt = build_prompt(self.bot.cfg, context, trigger, self.bot.chat_title, task["triggers"],
                                      memory=memory, account=self.bot.store.self_wxid, profile=profile)
                with self.bot.journal.db:
                    self.bot.journal.set('context', {**self.bot.journal.get('context', {}), 'reply':prompt.context_stats})
                echo = "[echo] " + "\n".join(m["text"] for m in task["triggers"]) if self.bot.echo else None
                timeout = float(model_settings(self.bot.cfg)['timeout_seconds']) + 15
                if self.bot.cfg.get("search", {}).get("enabled", True) and not self.bot.echo:
                    timeout = float(self.bot.cfg.get("search", {}).get("total_timeout_seconds", 120)) + 15
                images = []
                if self.bot.cfg.get("vision", {}).get("enabled", True) and not self.bot.echo:
                    for index, question in enumerate(task["triggers"]):
                        reference, depth = question.get("reference"), 0
                        while isinstance(reference, dict):
                            if reference.get("kind") == "image":
                                binding = self.bot.store.image_binding(self.bot.chat_id, reference)
                                if binding.get('download'):
                                    binding['download']['chat_title'] = self.bot.chat_title
                                images.append(dict(trigger_index=index, depth=depth, **binding))
                            reference, depth = reference.get("reference"), depth+1
                payload = (self.bot.cfg, prompt, echo)
                if images:
                    if self.bot.cfg['vision'].get('download_enabled', False):
                        timeout += self.bot.cfg['vision'].get('download_timeout_seconds', 20)
                        if self.bot.cfg['vision'].get('download_backend', 'desktop') == 'native':
                            task = self.bot.journal.reserve_native_images(task_id, version,
                                images[:self.bot.cfg['vision']['max_images']])
                    payload += (dict(context=context, triggers=task["triggers"], chat_title=self.bot.chat_title, images=images,
                                     memory=memory, account=self.bot.store.self_wxid,
                                     profile=profile),)
                if task.get('reply_expires_at') is not None:
                    timeout = min(timeout, max(0, task['reply_expires_at']-time.time()))
                with WorkerLease(self.bot.job_factory("generate", payload, timeout=timeout)) as lease:
                    with self.bot.journal.db:
                        task["generation"] = "running"
                        task["memory_used"] = dict(revision=memory_view.revision if memory_view and memory else None,
                                                   chars=len((memory or {}).get("text", "")))
                        self.bot.journal.put(task)
                    self.jobs[task_id] = (version, lease.job)
                    lease.transfer()
                started += 1
                self.bot.log.info("开始生成 task=%s version=%s questions=%s references=%s", task_id, version,
                         len(task["triggers"]), sum(isinstance(m.get("reference"), dict) for m in task["triggers"]))
            except WorkerCleanupError:
                raise
            except Exception as exc:
                self._failed(task_id, version, dict(error=getattr(exc,'code',type(exc).__name__)))
                self.bot.log.error("生成未启动 task=%s category=%s", task_id, type(exc).__name__)
        return started

    def _failed(self, task_id, version, result):
        failure = GenerationFailure.from_result(result)
        settings = self.bot.cfg['queue']
        if failure.code == 'context_required_too_large':
            with self.bot.journal.db:
                self.bot.journal.set('context', {**self.bot.journal.get('context', {}), 'reply':dict(error=failure.code)})
        self.bot.journal.generation_failure(task_id, version, failure.code, time.time(),
            retryable=failure.retryable, retries=settings['generation_retries'],
            delay=settings['generation_retry_delay_seconds'],
            fallback=format_reply('消息或引用内容过长，请分开发送。' if failure.code == 'context_required_too_large'
                                  else '刚才没接上，等会儿再问我一下。', self.bot.cfg))
        self.bot.log.warning('生成失败 task=%s version=%s category=%s retryable=%s',
                    task_id, version, failure.code, failure.retryable)
        native = next((m.get('native_finish_reason') for m in reversed(result.get('model_metrics') or [])
                       if m.get('role') == 'reply'), None)
        if isinstance(native, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', native):
            self.bot.log.warning('模型结束原因 task=%s native_finish_reason=%s', task_id, native)

    def close(self):
        with ExitStack() as stack:
            for _, job in self.jobs.values():
                stack.callback(job.close)
