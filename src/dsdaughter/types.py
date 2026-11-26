"""跨层共享的数据结构（通道无关）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    # 以下字段只在函数调用往返时出现
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            wire["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            wire["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            wire["reasoning_content"] = self.reasoning_content
        return wire


@dataclass(slots=True)
class Citation:
    url: str
    title: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "title": self.title}


@dataclass(slots=True)
class ToolCall:
    """模型要求调用的一次工具。arguments 是 JSON 字符串（OpenAI 格式）。"""

    id: str
    name: str
    arguments: str = "{}"


@dataclass(slots=True)
class ChatResult:
    """一次模型调用的最终结果。"""

    text: str = ""
    reasoning: str = ""
    citations: list[Citation] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class LLMEvent:
    """流式事件：text / thinking / tool / done。"""

    kind: Literal["text", "thinking", "tool", "done"]
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Turn:
    """已落库的一轮对话。"""

    role: Literal["user", "assistant"]
    content: str
    ts: datetime

    def to_message(self) -> Message:
        return Message(role=self.role, content=self.content)
