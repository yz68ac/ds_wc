"""External or provider-hosted search; no WeChat database or desktop access."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from wxbridge.ai.client import ModelError
from wxbridge.ai.output import format_reply
from wxbridge.ai.citations import without_citations
from provider import load_config
from provider.adapters.common import is_public_url_candidate
from wxbridge.ai.search_prompt import search_prompt

TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description":
        "搜索实时网页。用户明确要求上网，或最新新闻、现价、天气、近期更新等需要核实的信息应调用。普通闲聊、接梗、安慰、创作、翻译、纯数学无需搜索；明确询问梗的出处或含义且需要核实时再查。只用本次问题必要关键词，不传群聊记录、账号或无关个人信息。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "1至128字符的明确搜索词"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "web_fetch", "description":
        "需要阅读详情时抓取前面 web_search 返回的公开网页 URL；只允许本轮搜索已返回的链接。",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}}},
]


class SearchError(RuntimeError):
    pass


class SearchClient:
    def __init__(self, cfg):
        self.settings = cfg.get("search", {})
        config = load_config(cfg.get('provider_env'))
        connection = self.settings.get('connection', 'deepseek')
        self.api_key = config.api_key(connection)
        endpoint = config.connections[connection]
        self.base_url = endpoint.base_url.rstrip('/')
        if not self.api_key:
            raise SearchError("missing_api_key")
        self._opener = (urllib.request.build_opener(urllib.request.ProxyHandler({
            'http': endpoint.proxy_url, 'https': endpoint.proxy_url,
        })) if endpoint.proxy_url else None)

    def _post(self, tool, payload, deadline):
        open_request = self._opener.open if self._opener is not None else urllib.request.urlopen
        for attempt in range(2):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise SearchError("search_timeout")
            request = urllib.request.Request(self.base_url+"/v1/tools/"+tool,
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer "+self.api_key}, method="POST")
            try:
                with open_request(request, timeout=min(float(self.settings.get("timeout_seconds", 15)), remaining)) as response:
                    data = response.read(2*1024*1024+1)
                if len(data) > 2*1024*1024:
                    raise SearchError("search_response_too_large")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise SearchError("invalid_search_response")
                return result
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                code = "http_"+str(status)
                if attempt or status not in (408, 429) and status < 500:
                    raise SearchError(code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                code = "search_connection_failed"
                if attempt:
                    raise SearchError(code) from None
            except (ValueError, UnicodeError):
                raise SearchError("invalid_search_response") from None
            time.sleep(min(.5, max(0, deadline-time.monotonic())))
        raise SearchError(code)

    def search(self, query, deadline):
        return self._post("search", dict(query=query, count=self.settings.get("max_results", 5), need_content=False), deadline)

    def fetch(self, url, deadline):
        return self._post("fetch", dict(url=url), deadline)


def answer_with_search(cfg, client, messages, on_search=None, *, search_client=None, image_requests=None):
    """保留真实 assistant/tool 调用链；只报告一次内部进度事件，群提示由主进程控制。"""
    settings = cfg.get("search", {})
    image_enabled = cfg.get('image_generation', {}).get('enabled', False) and image_requests is not None
    search_enabled = settings.get('enabled', True)
    if image_enabled:
        from .image_generation import IMAGE_TOOL, image_tool_prompt, collect_image_request
        messages = image_tool_prompt(messages)
    backend_kind = settings.get('backend', 'external')
    if backend_kind == 'auto':
        backend_kind = 'native' if getattr(client.adapter, 'native_search_supported', False) and client.cfg.get('native_search_supported', True) else 'external'
    if backend_kind == 'native' and search_enabled:
        from .native_search import answer_with_native_search
        return answer_with_native_search(cfg, client, messages, on_search, image_requests=image_requests)
    if backend_kind not in ('external', 'native'):raise ModelError('unsupported_search_backend')
    deadline = time.monotonic()+float(settings.get("total_timeout_seconds", 120))
    from .output import response_prompt
    conversation = search_prompt(cfg, messages) if search_enabled else response_prompt(messages, cfg)
    tools = (TOOLS if search_enabled else []) + ([IMAGE_TOOL] if image_enabled else [])
    sources, errors = [], []
    calls = 0
    attempted = False
    backend = search_client

    def metadata():
        return dict(attempted=attempted, calls=calls, results=len(sources), errors=list(dict.fromkeys(errors)))

    def finish(text):
        text = format_reply(without_citations(text, sources), cfg)
        if not text.strip():
            raise ModelError('empty_reply', retryable=True)
        return text.strip(), metadata()

    for turn in range(int(settings.get("max_tool_rounds", 2))+1):
        exhausted = turn >= int(settings.get("max_tool_rounds", 2)) or calls >= int(settings.get("max_requests", 3))
        try:
            message = client.complete(conversation, tools=tools, tool_choice="none" if exhausted else "auto", deadline=deadline,
                                      require_complete=True)
        except ModelError:
            if not attempted:
                raise
            errors.append("answer_generation_failed")
            return finish("刚才查到了一些资料，但整理回答超时了，稍后再试。" if sources else "这次联网没能完成，我暂时核实不了，稍后再试。")
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not format_reply(message.get('content') or '', cfg).strip():
                return finish('')
            if attempted and not sources:
                return finish("这次没有查到可用的网页结果，暂时核实不了。你可以换个更具体的关键词，我再查。" if not errors else "这次联网查询没有完成，暂时核实不了，稍后再试。")
            return finish(message.get("content") or "")
        if not isinstance(tool_calls, list) or len(tool_calls) > 8:
            errors.append("invalid_tool_calls")
            return finish("这次查询指令异常，暂时没能完成，稍后再试。")
        if not exhausted and image_enabled and collect_image_request(tool_calls, image_requests):
            return finish('在画了，稍后发给你')
        if exhausted:
            errors.append("tool_limit")
            return finish("这次搜索已到查询上限，还没得到足够可靠的结论。" if not sources else "查到的资料还不足以确定答案，暂时给不了可靠结论。")
        conversation.append(client.assistant_message(message))
        for tool in tool_calls:
            output = None
            try:
                function = tool.get("function") or {}
                name = function.get("name")
                args = json.loads(function.get("arguments") or "{}")
                if not search_enabled or not isinstance(args, dict) or name not in ("web_search", "web_fetch"):
                    raise SearchError("invalid_tool")
                if calls >= int(settings.get("max_requests", 3)):
                    raise SearchError("tool_limit")
                if name == "web_search":
                    query = args.get("query")
                    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
                        raise SearchError("invalid_query")
                else:
                    url = args.get("url")
                    if not is_public_url_candidate(url) or url not in {source["url"] for source in sources}:
                        raise SearchError("unverified_fetch_url")
                if not attempted:
                    attempted = True
                    if on_search:
                        on_search()
                if backend is None:
                    backend = SearchClient(cfg)
                calls += 1
                if name == "web_search":
                    raw = backend.search(query.strip(), deadline)
                    rows = raw.get("results")
                    if not isinstance(rows, list):
                        raise SearchError("invalid_search_response")
                    found = []
                    for row in rows[:int(settings.get("max_results", 5))]:
                        if not isinstance(row, dict) or not is_public_url_candidate(row.get("url")):
                            continue
                        item = {key: str(row.get(key) or "")[:limit] for key, limit in
                                (("title", 200), ("url", 2048), ("summary", 1600), ("snippet", 400), ("date", 80))}
                        if not any(source["url"] == item["url"] for source in sources):
                            sources.append(dict(title=" ".join(item["title"].split()) or "网页来源", url=item["url"]))
                        found.append(item)
                    output = dict(results=found)
                else:
                    raw = backend.fetch(url, deadline)
                    output = dict(title=str(raw.get("title") or "")[:200], url=url,
                                  content=str(raw.get("content") or "")[:6000])
                    if not output["content"].strip():
                        raise SearchError("empty_webpage")
                    sources.sort(key=lambda item: item["url"] != url)
            except (SearchError, ValueError, TypeError) as exc:
                code = str(exc) if isinstance(exc, SearchError) else "invalid_tool_arguments"
                errors.append(code)
                output = dict(error=code, note="未获得可用材料，不得声称已经核实。")
            conversation.append(dict(role="tool", tool_call_id=str(tool.get("id") or ""),
                                     content=json.dumps(output, ensure_ascii=False)))
    return finish("这次查询没能得出可靠结论，稍后再试。")
