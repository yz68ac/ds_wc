"""DeepSeek OpenAI 兼容接口客户端（纯标准库）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from wxbridge.config import deepseek_api_key

MAX_REQUEST_BYTES = 48 * 1024 * 1024


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg["deepseek"]
        self.api_key = deepseek_api_key(cfg)
        if not self.api_key:
            env_name = self.cfg.get("api_key_env") or "DEEPSEEK_API_KEY"
            raise DeepSeekError(
                f"没有找到 DeepSeek API key。请设置环境变量 {env_name}，"
                "或写进项目根目录的 .env 文件："
                f"\n  {env_name}=sk-xxxxxxxx"
            )
        self.base_url = self.cfg["base_url"].rstrip("/")

    def _post(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise DeepSeekError("DeepSeek 请求超过 48 MiB 限制")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 服务端错误正文可能回显聊天内容，不写入日志。
            raise DeepSeekError(f"DeepSeek HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise DeepSeekError(f"DeepSeek 连接失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise DeepSeekError("DeepSeek 请求超时") from exc

    def chat(self, messages: list[dict[str, Any]]) -> str:
        payload = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg.get("temperature", 1.0),
            "max_tokens": self.cfg.get("max_tokens", 1200),
            "stream": False,
        }
        if self.cfg.get("thinking") in ("enabled", "disabled"):
            payload["thinking"] = {"type": self.cfg["thinking"]}
        timeout = int(self.cfg.get("timeout_seconds", 60))
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                data = self._post(payload, timeout)
                choices = data.get("choices") or []
                if not choices:
                    raise DeepSeekError("DeepSeek 返回异常：没有 choices")
                message = choices[0].get("message") or {}
                content = message.get("content") or ""
                return content.strip()
            except DeepSeekError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc or DeepSeekError("DeepSeek 调用失败")
