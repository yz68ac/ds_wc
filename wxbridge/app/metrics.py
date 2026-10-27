"""有界的阶段耗时和 API 计数；不接收正文、路径、昵称或密钥。"""
from contextlib import contextmanager
from copy import deepcopy
import time


class Metrics:
    def __init__(self):
        self.stages, self.models = {}, {}

    @contextmanager
    def measure(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            value = self.stages.setdefault(name, dict(count=0, last_ms=0, max_ms=0, total_ms=0))
            elapsed = round((time.monotonic()-started)*1000, 2)
            value.update(count=value['count']+1, last_ms=elapsed, max_ms=max(value['max_ms'], elapsed),
                         total_ms=round(value['total_ms']+elapsed, 2))

    def record_models(self, values):
        for item in values or []:
            role = item.get('role')
            if role not in ('reply', 'participation', 'memory', 'avatar'):
                continue
            entry = self.models.setdefault(role, dict(calls=0, errors=0, elapsed_ms=0,
                                                     prompt_tokens=0, completion_tokens=0))
            entry['calls'] += 1
            entry['errors'] += int(bool(item.get('error')))
            elapsed = item.get('elapsed_ms', 0)
            if type(elapsed) in (int, float) and 0 <= elapsed <= 3600000:
                entry['elapsed_ms'] = round(entry['elapsed_ms']+elapsed, 2)
            for key in ('prompt_tokens', 'completion_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'):
                value = (item.get('usage') or {}).get(key, 0)
                if type(value) is int and 0 <= value <= 10**9:
                    if key in entry or value:entry[key] = entry.get(key, 0)+value

    def snapshot(self):
        return deepcopy(dict(stages=self.stages, models=self.models))
