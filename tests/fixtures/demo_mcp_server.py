"""一个最小 MCP server（stdio），用来测试/演示 MCP 桥接。

它不是真的插件，只是让 `DSD_MCP_SERVERS` 有个可以连的东西：

    DSD_MCP_ENABLED=true
    DSD_MCP_SERVERS=demo=.venv/bin/python tests/fixtures/demo_mcp_server.py
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("demo", version="0.1.0")


@server.tool(description="把两个整数相加，返回它们的和")
def add(a: int, b: int) -> int:
    return int(a) + int(b)


@server.tool(description="把一段文字倒过来")
def reverse(text: str) -> str:
    return str(text)[::-1]


if __name__ == "__main__":
    server.run("stdio")
