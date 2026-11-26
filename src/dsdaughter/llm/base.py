"""LLM Provider 抽象。

为什么要有这一层：`deepseek-chat` 在 2026-07-24 被官方停用，模型名和接口
会变。把模型调用关在一个接口后面，换模型/换厂商只改一个文件。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from ..types import LLMEvent, Message


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、限流、内容审核拒绝等）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLM(Protocol):
    """所有 provider 实现同一套流式接口。"""

    def stream(
        self,
        messages: Sequence[Message],
        *,
        web: bool,
        thinking: bool,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """流式返回 text / thinking / tool 事件，最后一个事件是 kind="done"，
        payload["result"] 为 ChatResult。"""
        ...
