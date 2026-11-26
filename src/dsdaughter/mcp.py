"""MCP 桥接：把现成的 MCP server 变成她的工具（相当于"插件商店"）。

配置（`DSD_MCP_SERVERS`，多条用 `;` 分隔）：

    time=uvx mcp-server-time
    fetch=uvx mcp-server-fetch
    weather=http://127.0.0.1:9000/mcp

- stdio：`名字=命令 参数...`（会用子进程启动）
- HTTP ：`名字=https://...`（Streamable HTTP）

安全提醒：MCP server 是**任意可执行程序**，能读到本进程环境变量（API Key、QQ token、数据库密码）。
所以这个功能默认关闭，只接你信得过的 server；更稳的做法是把它跑在单独容器里再通过 HTTP 接。
"""

from __future__ import annotations

import functools
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import structlog

from .tools import Tool, ToolResult

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class McpServerSpec:
    name: str
    target: str
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class McpServerState:
    name: str
    tools: list[Any] = field(default_factory=list)
    error: str = ""


def parse_servers(raw: str) -> list[McpServerSpec]:
    """解析 `DSD_MCP_SERVERS`。

    语法：
        name=命令 参数...                      # stdio（子进程）
        name=https://host/mcp                   # Streamable HTTP
        name=sse:https://host/sse?key=xxx       # 旧版 SSE（国内 MCP 市场常见）
        name=sse:https://host/sse|Authorization: Bearer xxx   # 带请求头
        name=npx -y some-server|ENV:GITHUB_TOKEN=xxx          # 给子进程传环境变量
    """
    specs: list[McpServerSpec] = []
    for entry in re.split(r"[;\n]", raw or ""):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, rest = entry.partition("=")
        name = name.strip()
        parts = [part.strip() for part in rest.split("|")]
        target = parts[0] if parts else ""
        headers: dict[str, str] = {}
        env: dict[str, str] = {}
        for extra in parts[1:]:
            key, _, value = extra.partition(":")
            key, value = key.strip(), value.strip()
            if not key or not value:
                continue
            if key.upper() == "ENV":
                env_key, _, env_value = value.partition("=")
                if env_key.strip():
                    env[env_key.strip()] = env_value.strip()
            else:
                headers[key] = value
        if name and target:
            specs.append(McpServerSpec(name=name, target=target, headers=headers, env=env))
    return specs


def _safe(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)[:40]


def _format_result(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(item, 'type', 'content')}]")
    output = "\n".join(parts).strip()
    return output[:8000] if output else "(空结果)"


class McpManager:
    def __init__(self, servers: list[McpServerSpec], *, timeout: float = 30.0) -> None:
        self.servers = servers
        self.timeout = timeout
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, list[Any]] = {}
        self._states: dict[str, McpServerState] = {}
        self._stacks: list[AsyncExitStack] = []

    # ---------------------------------------------------------------- 启动
    async def start(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        for spec in self.servers:
            state = McpServerState(name=spec.name)
            self._states[spec.name] = state
            stack = AsyncExitStack()
            try:
                if spec.target.startswith("sse:"):
                    from mcp.client.sse import sse_client

                    read, write = await stack.enter_async_context(
                        sse_client(
                            spec.target[len("sse:") :],
                            headers=spec.headers or None,
                        )
                    )
                elif spec.target.startswith(("http://", "https://")):
                    from mcp.client.streamable_http import streamable_http_client

                    read, write = await stack.enter_async_context(
                        streamable_http_client(spec.target)
                    )
                else:
                    parts = shlex.split(spec.target)
                    if not parts:
                        raise ValueError("命令为空")
                    params = StdioServerParameters(
                        command=parts[0],
                        args=parts[1:],
                        # 传了自定义环境变量就合并完整环境，否则 SDK 会过滤掉（API Key 就没了）
                        env={**os.environ, **spec.env} if spec.env else None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))

                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=float(self.timeout))
                )
                await session.initialize()
                listed = await session.list_tools()
                state.tools = list(getattr(listed, "tools", []) or [])
                self._sessions[spec.name] = session
                self._tools[spec.name] = state.tools
                self._stacks.append(stack)
                log.info(
                    "MCP server 已连接",
                    server=spec.name,
                    tools=[tool.name for tool in state.tools],
                )
            except Exception as exc:
                state.error = str(exc)
                await stack.aclose()
                log.warning("MCP server 连接失败", server=spec.name, error=str(exc))

    # ---------------------------------------------------------------- 工具
    async def _call(self, ctx: Any, *, server: str, tool: str, **arguments: Any) -> ToolResult:
        session = self._sessions.get(server)
        if session is None:
            return ToolResult.failure(f"MCP server {server} 没连上")
        try:
            result = await session.call_tool(tool, arguments)
        except Exception as exc:
            return ToolResult.failure(f"MCP 工具调用失败：{exc}")
        text = _format_result(result)
        if getattr(result, "isError", False):
            return ToolResult.failure(text)
        return ToolResult.success(text, server=server, tool=tool)

    async def call_tool(self, server: str, tool: str, arguments: dict | None = None) -> str:
        """直接调一次 MCP 工具（探针脚本/调试用）。"""
        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server {server} 没连上")
        result = await session.call_tool(tool, arguments or {})
        return _format_result(result)

    def build_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for server, mcp_tools in self._tools.items():
            for mcp_tool in mcp_tools:
                # MCP SDK 2.x 用 input_schema，1.x 用 inputSchema，两个都兼容
                schema = (
                    getattr(mcp_tool, "input_schema", None)
                    or getattr(mcp_tool, "inputSchema", None)
                    or {}
                )
                if not isinstance(schema, dict):
                    schema = {}
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                description = str(getattr(mcp_tool, "description", "") or "")
                tools.append(
                    Tool(
                        name=f"mcp_{_safe(server)}_{_safe(mcp_tool.name)}",
                        description=f"[MCP:{server}] {description}".strip(),
                        parameters=schema,
                        handler=functools.partial(self._call, server=server, tool=mcp_tool.name),
                        is_action=True,
                    )
                )
        return tools

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.servers),
            "servers": [
                {
                    "name": state.name,
                    "connected": state.name in self._sessions,
                    "tools": [tool.name for tool in state.tools],
                    "error": state.error,
                }
                for state in self._states.values()
            ],
        }

    async def aclose(self) -> None:
        for stack in reversed(self._stacks):
            try:
                await stack.aclose()
            except Exception as exc:  # 退出阶段的异常不值得炸服务
                log.debug("MCP 关闭异常（忽略）", error=str(exc))
        self._stacks.clear()
        self._sessions.clear()
        self._tools.clear()
