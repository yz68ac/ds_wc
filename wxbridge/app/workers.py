"""可终止的工作进程：不创建 WeChatStore，不触碰解密数据库。"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from wxbridge.ai.client import capture_metrics, DeepSeekError


def _work(connection, kind, payload):
    # 第三方/GI 意外异常和 stdout 也不能把正文或密钥写到服务日志。
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink:
        import contextlib
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), capture_metrics() as metrics:
            try:
                if kind == "generate":
                    from wxbridge.ai.client import DeepSeekClient
                    cfg, prompt, echo_text = payload[:3]
                    vision = None
                    if echo_text is None and len(payload) > 3:
                        from wxbridge.ai.images import prepare_multimodal
                        prompt, vision = prepare_multimodal(cfg, prompt, payload[3])
                    search = None
                    if echo_text is not None:
                        answer = echo_text
                    elif cfg.get("search", {}).get("enabled", True):
                        from wxbridge.ai.search import answer_with_search
                        answer, search = answer_with_search(cfg, DeepSeekClient(cfg), prompt,
                            on_search=lambda: connection.send({"event": "search_started"}))
                    else:
                        from wxbridge.ai.output import format_reply, response_prompt
                        answer = DeepSeekClient(cfg).chat(response_prompt(prompt, cfg))
                        answer = format_reply(answer, cfg)
                    result = {"reply": answer}
                    if vision is not None:
                        result["vision"] = vision
                    if search is not None:
                        result["search"] = search
                elif kind == 'proactive':
                    from wxbridge.ai.proactive import evaluate
                    cfg, prompt = payload[:2]
                    options = payload[2] if len(payload) > 2 else {}
                    def reply_started(identifier):
                        connection.send(dict(event='proactive_reply_started', reply_to=identifier,
                                             model_metrics=list(metrics)))
                        metrics.clear()  # 判断用量在阶段事件中上报，最终结果只带生成用量。
                    result = {'decision': evaluate(cfg, prompt, source_ids=options.get('source_ids'),
                        timeout_seconds=options.get('timeout_seconds'), on_reply=reply_started)}
                elif kind == "memory":
                    from wxbridge.ai.memory import extract
                    cfg, snapshot = payload
                    result = {"proposal": extract(cfg, snapshot)}
                elif kind == "send":
                    from wxbridge.wechat.ui import Sender
                    cfg, chat, text = payload
                    result = {"ui": Sender(cfg).send(chat, text)}
                else:
                    raise ValueError("unknown_worker_kind")
                if metrics:
                    result['model_metrics'] = metrics
                connection.send({"ok": True, **result})
            except Exception as exc:
                # 只返回类别，异常消息可能包含输入参数。
                result = {"ok": False, "error": type(exc).__name__}
                if isinstance(exc, DeepSeekError):
                    result.update(error=exc.code, retryable=exc.retryable)
                if metrics:
                    result['model_metrics'] = metrics
                if kind == "send":
                    result["ui"] = {"error": getattr(exc, "code", "ui_unknown"),
                                    "operation": getattr(exc, "metadata", None)}
                    import traceback
                    from pathlib import Path
                    chain = []
                    current = exc
                    while current is not None and len(chain) < 4:
                        chain.append({"type": type(current).__name__,
                                      "frames": [{"file": Path(frame.filename).name, "line": frame.lineno,
                                                  "function": frame.name} for frame in traceback.extract_tb(current.__traceback__)[-6:]]})
                        current = current.__cause__
                    result["ui"]["trace"] = chain  # 不包含异常消息、源码行或变量。
                connection.send(result)
            finally:
                connection.close()


class ProcessJob:
    def __init__(self, kind, payload, *, timeout):
        ctx = mp.get_context("spawn")
        self.pipe, child = ctx.Pipe(duplex=False)
        self.process = ctx.Process(target=_work, args=(child, kind, payload), daemon=True)
        self.started = time.monotonic()
        self.timeout = timeout
        try:
            self.process.start()
        except BaseException:
            self.pipe.close()
            child.close()
            raise
        child.close()

    def poll(self):
        if self.pipe.poll():
            try:
                return self.pipe.recv()
            except EOFError:
                return {"ok": False, "error": "WorkerExited"}
        if not self.process.is_alive():
            return {"ok": False, "error": "WorkerExited"}
        if time.monotonic() - self.started > self.timeout:
            return {"ok": False, "error": "WorkerTimeout"}
        return None

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=0.2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=0.2)
        self.pipe.close()
