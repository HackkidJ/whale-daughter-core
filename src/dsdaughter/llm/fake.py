"""Fake provider：没有 API Key、或想零成本调试时使用。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..types import ChatResult, Citation, LLMEvent, Message, ToolCall


class FakeLLM:
    """可预测的假模型：用于测试、以及没配 Key 时先把链路跑通。"""

    def __init__(
        self,
        *,
        reply: str | None = None,
        delay: float = 0.0,
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        self._reply = reply
        self._delay = delay
        self._tool_calls = list(tool_calls or [])
        self.calls: list[list[Message]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        web: bool = False,
        thinking: bool = False,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        history = list(messages)
        self.calls.append(history)

        if self._tool_calls:
            pending = self._tool_calls
            self._tool_calls = []
            yield LLMEvent(
                "done",
                payload={
                    "result": ChatResult(
                        text="",
                        finish_reason="tool_calls",
                        tool_calls=pending,
                    )
                },
            )
            return
        user_text = next((m.content for m in reversed(history) if m.role == "user"), "")

        if thinking:
            yield LLMEvent("thinking", "（fake 模式：假装思考了一下）")

        if web:
            yield LLMEvent(
                "tool",
                payload={
                    "type": "call_started",
                    "step": 0,
                    "call": {
                        "type": "deepseek_web_search",
                        "kind": "search",
                        "action": {"type": "search", "queries": [user_text[:40]]},
                        "status": "completed",
                    },
                },
            )

        reply = (
            self._reply
            if self._reply is not None
            else f"（fake 模式）我听到你说「{user_text[:60]}」了。"
            "配上 DEEPSEEK_API_KEY 之后，我就能真的回答你啦。"
        )
        for start in range(0, len(reply), 8):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield LLMEvent("text", reply[start : start + 8])

        citations = (
            [Citation(url="https://example.com/fake-search", title="fake 模式示例来源")]
            if web
            else []
        )
        yield LLMEvent(
            "done",
            payload={
                "result": ChatResult(
                    text=reply,
                    citations=citations,
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    finish_reason="stop",
                )
            },
        )
