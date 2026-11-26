"""DeepSeek 官方 API 客户端（Chat Completions + 服务端联网工具）。

几个关键事实（2026-11 官方文档）：
- 服务端联网工具通过 `tools: [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}]`
  声明，由服务端执行，**调用本身不计费**，只算 token。
- 联网工具不能和其他工具（函数调用）同时声明；也暂不能与 `safety_check` 同开。
- 流式时工具进度在 `choices[0].server_tool_event`，最后一次 chunk 的 delta 里
  才有完整的 `server_tool_calls` 与 `annotations`（引用）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx
import structlog

from ..config import Settings
from ..types import ChatResult, Citation, LLMEvent, Message, ToolCall
from .base import LLMError

log = structlog.get_logger(__name__)

_WEB_TOOL_TYPES = ("deepseek_web_search", "deepseek_read_url")


def _normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    """OpenAI 风格 usage 摊平成 {key: int}，便于日志和展示。"""
    flat: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            flat[key] = value
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, int) and not isinstance(sub_value, bool):
                    flat[f"{key}.{sub_key}"] = sub_value
    return flat


def _citations_from(annotations: Sequence[Any], server_tool_calls: Sequence[Any]) -> list[Citation]:
    """引用优先取 annotations；没有就从工具调用的 sources / page 里补。"""
    found: list[Citation] = []
    seen: set[str] = set()

    def add(url: Any, title: Any = "") -> None:
        url_text = str(url or "").strip()
        if not url_text or url_text in seen:
            return
        seen.add(url_text)
        found.append(Citation(url=url_text, title=str(title or "").strip()))

    for item in annotations or ():
        if not isinstance(item, dict):
            continue
        payload = item.get("url_citation") or item
        if isinstance(payload, dict):
            add(payload.get("url"), payload.get("title"))

    for step in server_tool_calls or ():
        if not isinstance(step, dict):
            continue
        for call in step.get("calls") or ():
            if not isinstance(call, dict):
                continue
            page = call.get("page")
            if isinstance(page, dict):
                add(page.get("url"), page.get("title"))
            for source in call.get("sources") or ():
                if isinstance(source, dict):
                    add(source.get("url"), source.get("title"))
    return found


class DeepSeekLLM:
    """OpenAI 兼容的 Chat Completions 流式客户端。"""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if not settings.deepseek_api_key:
            raise LLMError("缺少 DEEPSEEK_API_KEY")
        self._settings = settings
        self._api_key = settings.deepseek_api_key
        self._base_url = settings.base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=settings.request_timeout, write=30.0, pool=10.0
            )
        )
        if settings.safety_check and settings.web_tools:
            log.warning(
                "safety_check 与联网工具互斥，联网工具已自动关闭",
                safety_check=settings.safety_check,
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ payload
    def _build_payload(
        self,
        messages: Sequence[Message],
        *,
        web: bool,
        thinking: bool,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # 官方限制：safety_check 与联网工具暂时不能同开
        web = web and self._settings.web_tools and not self._settings.safety_check
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [m.to_wire() for m in messages],
            "stream": True,
            "temperature": self._settings.temperature,
        }
        if self._settings.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if self._settings.max_tokens:
            payload["max_tokens"] = self._settings.max_tokens
        # 思考模式不支持这两个参数（传了也会被忽略），非思考模式用来压"AI 复读感"
        if not thinking:
            if self._settings.presence_penalty is not None:
                payload["presence_penalty"] = self._settings.presence_penalty
            if self._settings.frequency_penalty is not None:
                payload["frequency_penalty"] = self._settings.frequency_penalty
        if tools:
            # 函数调用与服务端联网工具不能同时声明（官方限制），由调用方保证二选一
            payload["tools"] = list(tools)
        elif web:
            payload["tools"] = [{"type": tool} for tool in _WEB_TOOL_TYPES]
        # 思考模式默认是开的，必须显式关掉，否则 temperature 会被忽略、响应也变慢
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if thinking and self._settings.reasoning_effort:
            payload["reasoning_effort"] = self._settings.reasoning_effort
        if self._settings.safety_check:
            payload["safety_check"] = {"level": self._settings.safety_check}
        return payload

    # ------------------------------------------------------------------- stream
    async def stream(
        self,
        messages: Sequence[Message],
        *,
        web: bool = False,
        thinking: bool = False,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        payload = self._build_payload(messages, web=web, thinking=thinking, tools=tools)
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, int] = {}
        finish_reason = "stop"
        annotations: list[Any] = []
        server_tool_calls: list[Any] = []
        pending_tool_calls: dict[int, dict[str, str]] = {}

        try:
            async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise LLMError(
                        f"DeepSeek HTTP {response.status_code}: {body[:500]}",
                        status_code=response.status_code,
                    )
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("跳过无法解析的流式数据", data=data[:200])
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"DeepSeek 流内错误：{chunk['error']}")
                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        usage = _normalize_usage(chunk["usage"])
                    for choice in chunk.get("choices") or ():
                        delta = choice.get("delta") or {}
                        reasoning_delta = delta.get("reasoning_content")
                        if reasoning_delta:
                            reasoning_parts.append(reasoning_delta)
                            yield LLMEvent("thinking", reasoning_delta)
                        content_delta = delta.get("content")
                        if content_delta:
                            text_parts.append(content_delta)
                            yield LLMEvent("text", content_delta)
                        if delta.get("server_tool_calls"):
                            server_tool_calls = delta["server_tool_calls"]
                        for raw_call in delta.get("tool_calls") or ():
                            if not isinstance(raw_call, dict):
                                continue
                            index = int(raw_call.get("index") or 0)
                            slot = pending_tool_calls.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if raw_call.get("id"):
                                slot["id"] = str(raw_call["id"])
                            function = raw_call.get("function") or {}
                            if function.get("name"):
                                slot["name"] = str(function["name"])
                            if function.get("arguments"):
                                slot["arguments"] += str(function["arguments"])
                        if delta.get("annotations"):
                            annotations = delta["annotations"]
                        tool_event = choice.get("server_tool_event")
                        if tool_event:
                            yield LLMEvent("tool", payload=tool_event)
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek 请求失败：{exc}") from exc

        if finish_reason == "content_filter":
            log.warning("回复被内容安全拦截", finish_reason=finish_reason)
        tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
            for index, slot in sorted(pending_tool_calls.items())
            if slot["name"]
        ]
        result = ChatResult(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            citations=_citations_from(annotations, server_tool_calls),
            usage=usage,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
        yield LLMEvent("done", payload={"result": result})
