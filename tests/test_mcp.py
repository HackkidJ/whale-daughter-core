"""B：MCP 桥接 —— 配置解析 + 真连一个 stdio MCP server 调工具。"""

from __future__ import annotations

import sys
from pathlib import Path

from dsdaughter.config import Settings
from dsdaughter.mcp import McpManager, parse_servers
from dsdaughter.tools import ToolContext

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo_mcp_server.py"


def test_parse_servers():
    specs = parse_servers("time=uvx mcp-server-time; fetch=http://127.0.0.1:9000/mcp")
    assert [spec.name for spec in specs] == ["time", "fetch"]
    assert specs[0].target == "uvx mcp-server-time"
    assert specs[1].target == "http://127.0.0.1:9000/mcp"


def test_parse_servers_transports_and_extras():
    specs = parse_servers(
        "time=uvx mcp-server-time|ENV:UV_TOOL_DIR=/tmp/uvtools;"
        " s=sse:https://example.com/sse|Authorization: Bearer abc;"
        " h=https://example.com/mcp"
    )
    assert [spec.name for spec in specs] == ["time", "s", "h"]
    assert specs[0].env == {"UV_TOOL_DIR": "/tmp/uvtools"}
    assert specs[1].target == "sse:https://example.com/sse"
    assert specs[1].headers == {"Authorization": "Bearer abc"}
    assert specs[2].target == "https://example.com/mcp"


def test_parse_servers_ignores_junk():
    assert parse_servers("") == []
    assert parse_servers("没有等号; =空名字; 空目标=") == []


async def test_manager_connects_lists_and_calls_tools():
    specs = parse_servers(f"demo={sys.executable} {FIXTURE}")
    manager = McpManager(specs, timeout=20)
    await manager.start()
    try:
        tools = manager.build_tools()
        names = {tool.name for tool in tools}
        assert "mcp_demo_add" in names
        assert "mcp_demo_reverse" in names

        add_spec = next(tool for tool in tools if tool.name == "mcp_demo_add")
        assert add_spec.parameters.get("required") == ["a", "b"]
        assert set(add_spec.parameters.get("properties", {})) == {"a", "b"}

        add = next(tool for tool in tools if tool.name.endswith("_add"))
        ctx = ToolContext(settings=Settings(_env_file=None, memory_extract=False), session_id="t")
        result = await add.handler(ctx, a=12, b=30)
        assert result.ok is True
        assert "42" in result.content

        reverse = next(tool for tool in tools if tool.name.endswith("_reverse"))
        second = await reverse.handler(ctx, text="abc")
        assert second.ok is True
        assert "cba" in second.content

        status = manager.status()
        assert status["servers"][0]["connected"] is True
        assert "add" in status["servers"][0]["tools"]
    finally:
        await manager.aclose()


async def test_manager_reports_bad_server_without_crashing():
    specs = parse_servers(f"broken={sys.executable} /nonexistent/mcp_server.py")
    manager = McpManager(specs, timeout=5)
    await manager.start()
    try:
        assert manager.build_tools() == []
        status = manager.status()
        assert status["servers"][0]["connected"] is False
        assert status["servers"][0]["error"]
    finally:
        await manager.aclose()
