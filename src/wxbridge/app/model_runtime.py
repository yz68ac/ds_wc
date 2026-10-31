"""Per-group serial model lanes; results are applied only by the owner loop."""
from concurrent.futures import Future, CancelledError
from copy import deepcopy
from queue import Queue, Empty
from threading import Thread, Lock, Event
import time
from provider import ModelError, create_client, capture_metrics, load_config
from wxbridge.ai.client import _scope, client_for

class ModelJob:

    def __init__(self, function, timeout):
        self.function, self.deadline = (function, time.monotonic() + timeout)
        self.future, self.events, self.cancelled = (Future(), Queue(), Event())
        self.delivered = False

    def check(self):
        if self.cancelled.is_set():
            raise CancelledError()
        if time.monotonic() >= self.deadline:
            raise ModelError('deadline_exceeded', retryable=True)

    def poll(self):
        if self.delivered:
            return None
        try:
            return self.events.get_nowait()
        except Empty:
            pass
        if self.future.done():
            self.delivered = True
            return dict(ok=False, error='cancelled') if self.future.cancelled() else self.future.result()
        if time.monotonic() >= self.deadline:
            self.close()
            self.delivered = True
            return dict(ok=False, error='WorkerTimeout', retryable=True)
        return None

    def close(self):
        self.cancelled.set()
        self.future.cancel()

class ModelLane:

    def __init__(self, name, connection, config):
        self.name, self.connection, self.config = (name, connection, config)
        self.client, self.queue, self.active = (None, Queue(), None)
        self.thread, self.closed, self.lock = (None, False, Lock())

    def get_client(self):
        if self.client is None:
            self.client = create_client(self.connection, config=self.config)
        return self.client

    def submit(self, function, timeout):
        with self.lock:
            if self.closed:
                raise ModelError('client_closed')
            job = ModelJob(function, timeout)
            self.queue.put(job)
            if self.thread is None:
                self.thread = Thread(target=self._run, name='model-' + self.name)
                self.thread.start()
            return job

    def _run(self):
        while True:
            job = self.queue.get()
            if job is None:
                break
            with self.lock:
                self.active = job
                if self.closed:
                    job.close()
            try:
                if job.future.set_running_or_notify_cancel():
                    try:
                        job.check()
                        value = job.function(job)
                        job.check()
                    except Exception as exc:
                        value = dict(ok=False, error=getattr(exc, 'code', type(exc).__name__), retryable=getattr(exc, 'retryable', False))
                    job.future.set_result(value)
            finally:
                with self.lock:
                    self.active = None
                self.queue.task_done()

    def begin_close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                if self.active:
                    self.active.close()
                while True:
                    try:
                        job = self.queue.get_nowait()
                    except Empty:
                        break
                    job.close()
                    self.queue.task_done()
                self.queue.put(None)

    def stopped(self):
        return self.thread is None or not self.thread.is_alive()

    def close(self):
        self.begin_close()
        if self.thread:
            self.thread.join()
        if self.client:
            self.client.close()

class GroupModels:

    def __init__(self, cfg):
        self.cfg = deepcopy(cfg)
        config = load_config(cfg.get('provider_env'))
        self.lanes = {name: ModelLane(name, connection, config) for name, connection in cfg['model_clients'].items()}

    def submit(self, kind, payload, *, timeout):
        lane = self.lanes['reply' if kind == 'generate' or (kind == 'request' and payload[1] == 'reply') else 'background']
        payload = deepcopy(payload)

        def execute(job):
            with capture_metrics() as metrics:
                token = _scope.set((lane, self.cfg, job, metrics))
                try:
                    return dict(ok=True, **self._execute(kind, payload, job), model_metrics=metrics)
                except Exception as exc:
                    result = dict(ok=False, error=getattr(exc, 'code', type(exc).__name__), retryable=getattr(exc, 'retryable', False), model_metrics=metrics)
                    if kind == 'participation':
                        from wxbridge.app.task_policy import participation_failure_code
                        result['error'] = participation_failure_code(result)
                    return result
                finally:
                    _scope.reset(token)
        return lane.submit(execute, timeout)

    def _execute(self, kind, payload, job):
        cfg = self.cfg
        if kind == 'generate':
            _, prompt, echo = payload[:3]
            vision = search = None
            if len(payload) > 3 and echo is None:
                from wxbridge.app.workers import ProcessJob
                prep = ProcessJob('prepare_images', (cfg, prompt, payload[3]), timeout=max(1, job.deadline - time.monotonic()))
                try:
                    while (value := prep.poll()) is None:
                        job.check()
                        time.sleep(0.05)
                    if not value['ok']:
                        raise ModelError(value['error'])
                    prompt, vision = (value['prompt'], value['vision'])
                finally:
                    prep.close()
            image_requests = []
            if echo is not None:
                answer = echo
            elif cfg['search']['enabled'] or cfg.get('image_generation', {}).get('enabled', False):
                from wxbridge.ai.search import answer_with_search
                answer, search = answer_with_search(cfg, client_for('reply'), prompt,
                    on_search=lambda: job.events.put(dict(event='search_started')),
                    **(dict(image_requests=image_requests) if cfg.get('image_generation', {}).get('enabled', False) else {}))
            else:
                from wxbridge.ai.output import format_reply, response_prompt
                answer = format_reply(client_for('reply').chat(response_prompt(prompt, cfg)), cfg)
            if not answer.strip():
                raise ModelError('empty_reply', retryable=True)
            return dict(reply=answer, vision=vision, search=search, image_requests=image_requests)
        if kind == 'request':
            return dict(result=client_for(payload[1]).request(payload[2]))
        if kind == 'memory':
            from wxbridge.ai.memory import extract
            return dict(proposal=extract(cfg, payload[1]))
        if kind == 'group_profile':
            from wxbridge.ai.group_profile import analyze
            return dict(proposal=analyze(cfg, payload[1], deadline=job.deadline))
        if kind == 'participation':
            from wxbridge.ai.participation import evaluate
            return dict(decision=evaluate(cfg, payload[1], **payload[2]))
        raise ValueError('unknown_model_job')

    def submit_images(self, request, *, deadline=None):
        lane = self.lanes['image']
        request = deepcopy(request)
        timeout = request.options.get('timeout_seconds', 300)
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        result = Future()

        def execute(job):
            if not result.set_running_or_notify_cancel():
                return {}
            failure = None
            metrics = []
            try:
                with capture_metrics() as metrics:
                    value = lane.get_client().generate_images(request, deadline=job.deadline)
                    job.check()
            except Exception as exc:
                failure = exc if isinstance(exc, ModelError) else ModelError('image_generation_failed')
            for entry in metrics:
                entry.update(role='image', channel='image')
            result.model_metrics = metrics
            if failure is not None:
                result.set_exception(failure)
            else:
                result.set_result(value)
            return {}
        job = lane.submit(execute, timeout)
        result.cancel_request = job.close
        result.add_done_callback(lambda f: job.close() if f.cancelled() else None)

        def complete(f):
            if not result.done():
                result.set_exception(CancelledError() if f.cancelled() else ModelError('deadline_exceeded'))
        job.future.add_done_callback(complete)
        return result

    def status(self):
        return {name: dict(running=lane.active is not None, queued=max(0,lane.queue.qsize()-int(lane.closed)), client_created=lane.client is not None) for name, lane in self.lanes.items()}

    def begin_close(self):
        for lane in self.lanes.values():
            lane.begin_close()

    def stopped(self):
        return all(lane.stopped() for lane in self.lanes.values())

    def close(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for lane in self.lanes.values():
                stack.callback(lane.close)
