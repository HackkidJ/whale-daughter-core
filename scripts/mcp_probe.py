#!/usr/bin/env python3
"""MCP server 探针：加进 .env 之前先看看它能干什么。

用法：

    # stdio（推荐，包管理器会自己下）
    uv run python scripts/mcp_probe.py "time=uvx mcp-server-time"

    # Streamable HTTP
    uv run python scripts/mcp_probe.py "remote=https://example.com/mcp"

    # 旧版 SSE（国内 MCP 市场常见；可带请求头）
    uv run python scripts/mcp_probe.py \
        "maps=sse:https://mcp.amap.com/sse?key=你的key|Authorization: Bearer xxx"

    # 需要给子进程传环境变量（比如 API Key）
    uv run python scripts/mcp_probe.py \
        "gh=npx -y server-github|ENV:GITHUB_TOKEN=xxx"

    # 顺便试调一次工具（工具名用 MCP 原始名，不带 mcp_ 前缀）
    uv run python scripts/mcp_probe.py "time=uvx mcp-server-time" --call get_current_time
    uv run python scripts/mcp_probe.py "demo=.venv/bin/python tests/fixtures/demo_mcp_server.py" \
        --call add --args '{"a": 1, "b": 2}'

探通之后，把同一串 `name=target` 写进 `.env` 的 `DSD_MCP_SERVERS`，重启 bot 即可。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from dsdaughter.mcp import McpManager, parse_servers


async def run(args: argparse.Namespace) -> int:
    specs = parse_servers(args.server)
    if not specs:
        print("解析不到 server 配置，格式应该是 name=命令/URL", file=sys.stderr)
        return 2

    manager = McpManager(specs, timeout=args.timeout)
    await manager.start()
    try:
        status = manager.status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if not manager._tools:  # noqa: SLF001 - 探针脚本就是用来检查内部状态的
            print("\n一个工具都没列出来：可能 transport/鉴权不对。", file=sys.stderr)
            return 1

        tools = manager.build_tools()
        for tool in tools:
            print(f"\n- {tool.name}")
            print(f"  说明：{tool.description[:200]}")
            print(f"  参数：{json.dumps(tool.parameters, ensure_ascii=False)}")

        if args.call:
            raw_args = json.loads(args.args or "{}")
            server = specs[0].name
            print(f"\n调用 {args.call}({raw_args}) ...")
            print(await manager.call_tool(server, args.call, raw_args))
        return 0
    finally:
        await manager.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP server 探针：连接并列出工具")
    parser.add_argument("server", help="name=target，例如 time=uvx mcp-server-time")
    parser.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数")
    parser.add_argument("--call", help="试调用一次工具（MCP 原始工具名）")
    parser.add_argument("--args", default="{}", help="调用参数 JSON")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
