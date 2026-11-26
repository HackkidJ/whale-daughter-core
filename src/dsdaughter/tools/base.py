"""工具层：把"她会做的事"变成模型可以调用的函数。

设计约束（重要）：
DeepSeek 的服务端联网工具**不能和函数调用同时声明**，所以一旦启用工具模式，
联网就改成客户端工具 `web_search` / `fetch_url`（走 Search / Fetch API）。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class ToolError(RuntimeError):
    """工具内部可预期的失败（会把原因回给模型，让它自己决定怎么圆）。"""


@dataclass(slots=True)
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **data: Any) -> ToolResult:
        return cls(ok=True, content=content, data=data)

    @classmethod
    def failure(cls, content: str) -> ToolResult:
        return cls(ok=False, content=content)


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    is_action: bool = False  # 会改变外部世界 → 走配额与冷却

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolContext:
    """一次工具调用需要的运行时信息。"""

    settings: Any
    session_id: str
    qq: Any | None = None

    def require_qq(self) -> Any:
        if self.qq is None:
            raise ToolError("QQ 通道没有接入，这个动作现在做不了")
        if not self.qq.server.connected:
            raise ToolError("QQ 现在没连上（NapCat 掉线了），稍后再试")
        return self.qq


class ActionGuard:
    """动作类工具的配额：每日上限 + 最小间隔，避免她刷屏或被风控。"""

    def __init__(self, limits: dict[str, int], cooldown: float) -> None:
        self._limits = limits
        self._cooldown = cooldown
        self._counts: dict[str, int] = defaultdict(int)
        self._last: dict[str, float] = {}
        self._day = time.strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._counts.clear()

    def check(self, name: str) -> str | None:
        self._roll_day()
        limit = self._limits.get(name)
        if limit is not None and self._counts[name] >= limit:
            return f"今天已经用过 {limit} 次 {name} 了，明天再说"
        last = self._last.get(name)
        if last is not None and self._cooldown > 0:
            remain = self._cooldown - (time.monotonic() - last)
            if remain > 0:
                return f"刚做过，{int(remain) + 1} 秒后再说"
        return None

    def record(self, name: str) -> None:
        self._roll_day()
        self._counts[name] += 1
        self._last[name] = time.monotonic()


class ToolRegistry:
    def __init__(self, *, guard: ActionGuard | None = None, history_size: int = 50) -> None:
        self._tools: dict[str, Tool] = {}
        self._guard = guard or ActionGuard({}, 0.0)
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)

    # ---------------------------------------------------------------- 注册
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: Sequence[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # ---------------------------------------------------------------- 调用
    async def call(
        self, name: str, arguments: str | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            result = ToolResult.failure(f"没有叫 {name} 的工具，可用：{', '.join(self._tools)}")
            self._remember(name, result, {})
            return result

        if isinstance(arguments, str):
            try:
                args = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return ToolResult.failure("参数不是合法 JSON，请重新给出参数")
            if not isinstance(args, dict):
                return ToolResult.failure("参数必须是 JSON 对象")
        else:
            args = dict(arguments)

        if tool.is_action:
            blocked = self._guard.check(name)
            if blocked is not None:
                result = ToolResult.failure(blocked)
                self._remember(name, result, args)
                return result

        try:
            result = await tool.handler(ctx, **args)
        except ToolError as exc:
            result = ToolResult.failure(str(exc))
        except TypeError as exc:
            result = ToolResult.failure(f"参数不对：{exc}")
        except Exception as exc:  # 工具故障不能把主流程带崩
            log.error("工具执行异常", tool=name, error=str(exc))
            result = ToolResult.failure(f"工具执行失败：{exc}")

        if tool.is_action and result.ok:
            self._guard.record(name)
        self._remember(name, result, args)
        return result

    def _remember(self, name: str, result: ToolResult, args: dict[str, Any]) -> None:
        self.history.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": name,
                "ok": result.ok,
                "args": args,
                "content": result.content[:200],
            }
        )
        log.info("tool_called", tool=name, ok=result.ok, args=args, preview=result.content[:120])
