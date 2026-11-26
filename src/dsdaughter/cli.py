"""CLI：在终端里直接和她聊天（开发调试最快的方式）。"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from .agent import Agent
from .config import Settings, get_settings
from .llm import LLMError
from .logging_setup import configure_cli_logging

log = structlog.get_logger(__name__)


def _build_settings(args: argparse.Namespace) -> Settings:
    base = get_settings()
    updates: dict[str, object] = {}
    if args.fake:
        updates["llm_mode"] = "fake"
    if args.no_web:
        updates["web_tools"] = False
    if args.thinking:
        updates["thinking"] = True
    if args.model:
        updates["model"] = args.model
    return base.model_copy(update=updates) if updates else base


async def _chat_once(agent: Agent, session_id: str, text: str) -> str:
    printed = False
    reply = ""
    async for event in agent.stream_reply(session_id, text):
        if event.kind == "text":
            print(event.text, end="", flush=True)
            printed = True
        elif event.kind == "tool":
            payload = event.payload or {}
            if payload.get("type") == "function_call":
                print(f"\n  · {payload.get('name')}…", flush=True)
            elif payload.get("type") == "function_result":
                if not payload.get("ok"):
                    print(f"    （没成功：{str(payload.get('preview') or '')[:80]}）", flush=True)
            else:
                kind = (payload.get("call") or {}).get("kind", "search")
                print(f"\n  · {'搜索中' if kind == 'search' else '读取网页'}…", flush=True)
        elif event.kind == "done":
            result = event.payload["result"]
            reply = result.text
            if printed:
                print()
            for citation in result.citations:
                print(f"  ↳ {citation.title or citation.url}  {citation.url}")
    return reply


async def _interactive(agent: Agent, session_id: str) -> None:
    print(f"和 {agent.persona.name} 聊天（输入 /quit 退出，/reset 清空记忆）")
    while True:
        try:
            line = await asyncio.to_thread(input, "\n你 > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return
        if text == "/reset":
            agent.reset(session_id)
            print("（记忆已清空）")
            continue
        try:
            await _chat_once(agent, session_id, text)
        except LLMError as exc:
            print(f"\n[模型调用失败] {exc}")
        except Exception as exc:  # 交互模式下不要因为一次错误就退出
            print(f"\n[出错了] {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dsd", description="和你的赛博女儿聊天")
    parser.add_argument("--once", metavar="TEXT", help="只发一条消息然后退出")
    parser.add_argument("--session", default="cli", help="会话 id（不同会话记忆隔离）")
    parser.add_argument("--fake", action="store_true", help="强制使用 fake 模型（零成本调试）")
    parser.add_argument("--no-web", action="store_true", help="关闭联网工具")
    parser.add_argument("--thinking", action="store_true", help="开启思考/深度搜索模式")
    parser.add_argument("--model", help="覆盖模型名，例如 deepseek-v4-pro")
    parser.add_argument("--verbose", action="store_true", help="打开调试日志")
    args = parser.parse_args(argv)

    configure_cli_logging(args.verbose)
    settings = _build_settings(args)
    try:
        agent = Agent.from_settings(settings)
    except FileNotFoundError as exc:
        print(f"找不到人设文件：{exc}\n提示：在仓库根目录运行，或设置 DSD_PERSONA_PATH")
        return 2

    async def _run() -> None:
        try:
            if args.once:
                await _chat_once(agent, args.session, args.once)
            else:
                await _interactive(agent, args.session)
        finally:
            await agent.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
