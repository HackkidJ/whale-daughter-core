"""FastAPI 入口：`uvicorn dsdaughter.main:app`（容器里也是它）。"""

from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .agent import Agent
from .api import create_router
from .behavior.clock import BioClock
from .behavior.context import BehaviorContext
from .behavior.diary import DiaryWriter
from .behavior.scheduler import BehaviorScheduler
from .behavior.store import BehaviorStore
from .channels import QQChannel
from .channels.qq import create_router as create_qq_router
from .config import Settings, get_settings
from .logging_setup import configure_logging
from .mcp import McpManager, parse_servers
from .phone.peeker import PhonePeeker, TaobaoBrowser
from .proactive import ProactiveScheduler

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None, agent: Agent | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    agent = agent or Agent.from_settings(settings)

    router = create_router(agent)
    qq = None
    scheduler = None
    if settings.qq_enabled:
        qq = QQChannel(agent=agent, settings=settings)
        agent.attach_qq(qq)
        if settings.proactive_enabled:
            scheduler = ProactiveScheduler(agent=agent, channel=qq, settings=settings)

    behavior: BehaviorScheduler | None = None
    behavior_store: BehaviorStore | None = None
    behavior_clock = None
    if qq is not None and settings.behavior_enabled:
        behavior_store = BehaviorStore(settings.behavior_db_path)
        clock = BioClock(settings)
        behavior_clock = clock
        behavior = BehaviorScheduler(
            agent=agent,
            channel=qq,
            settings=settings,
            store=behavior_store,
            clock=clock,
            diary=DiaryWriter(agent, behavior_store, settings),
        )
        agent.register_context_provider(
            BehaviorContext(behavior_store, clock, settings).prompt_block
        )
        agent.set_pre_reply_hook(behavior.on_user_message)

    peeker: PhonePeeker | None = None
    taobao: TaobaoBrowser | None = None
    if qq is not None and settings.phone_enabled:
        if settings.phone_peek_enabled:
            peeker = PhonePeeker(agent=agent, channel=qq, settings=settings)
        if settings.taobao_enabled:
            taobao = TaobaoBrowser(agent=agent, channel=qq, settings=settings)

    mcp_manager: McpManager | None = None
    if settings.mcp_enabled:
        specs = parse_servers(settings.mcp_servers)
        if specs:
            mcp_manager = McpManager(specs, timeout=settings.mcp_timeout)
        else:
            log.warning("DSD_MCP_ENABLED=true 但 DSD_MCP_SERVERS 为空")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if mcp_manager is not None:
            await mcp_manager.start()
            added = agent.set_extra_tools(mcp_manager.build_tools())
            if added:
                log.info("MCP 工具已挂载", tools=added)
        task: asyncio.Task[None] | None = None
        if scheduler is not None:
            task = asyncio.create_task(scheduler.run())
        extra_tasks: list[asyncio.Task[None]] = []
        if behavior is not None:
            extra_tasks.append(asyncio.create_task(behavior.run()))
        if peeker is not None:
            extra_tasks.append(asyncio.create_task(peeker.run()))
        if taobao is not None:
            extra_tasks.append(asyncio.create_task(taobao.run()))
        plugin_task: asyncio.Task[None] | None = None
        if settings.plugins_enabled:
            plugin_task = asyncio.create_task(agent.watch_plugins())
        try:
            yield
        finally:
            if plugin_task is not None:
                plugin_task.cancel()
                await asyncio.gather(plugin_task, return_exceptions=True)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for extra in extra_tasks:
                extra.cancel()
            if extra_tasks:
                await asyncio.gather(*extra_tasks, return_exceptions=True)
            if mcp_manager is not None:
                await mcp_manager.aclose()
            if scheduler is not None:
                await scheduler.aclose()
            if behavior_store is not None:
                await behavior_store.aclose()
            await agent.aclose()

    app = FastAPI(
        lifespan=lifespan,
        title="DSDaughter",
        description="赛博女儿 · 人格 / 联网 / QQ / 自己动手 / 记忆 / 主动性",
        version="0.3.0",
    )
    app.state.peeker = peeker
    app.state.taobao = taobao
    app.state.behavior_store = behavior_store
    app.state.behavior_clock = behavior_clock

    async def _diary_payload(limit: int = 30) -> dict[str, Any]:
        if behavior_store is None:
            return {"enabled": False, "diaries": [], "emotion": None}
        diaries = await behavior_store.recent_diaries(limit)
        emotion = await behavior_store.get_emotion()
        proactive_today = await behavior_store.count_proactive_today()
        clock = behavior_clock
        now = datetime.now(clock.tz) if clock is not None else datetime.now()
        return {
            "enabled": True,
            "now": now.isoformat(timespec="minutes"),
            "is_asleep": bool(clock.is_asleep(now)) if clock is not None else False,
            "sleep_window": list(clock.sleep_window(now)) if clock is not None else [],
            "diaries": [
                {
                    "day": diary.day,
                    "content": diary.content,
                    "created_at": diary.created_at,
                }
                for diary in diaries
            ],
            "emotion": {
                "mood": emotion.mood,
                "energy": emotion.energy,
                "reason": emotion.reason,
                "updated_at": emotion.updated_at,
            },
            "proactive_today": proactive_today,
        }

    @app.get("/api/behavior/diary")
    async def behavior_diary() -> dict[str, Any]:
        return await _diary_payload()

    @app.get("/api/behavior/state")
    async def behavior_state() -> dict[str, Any]:
        payload = await _diary_payload(limit=1)
        payload.pop("diaries", None)
        return payload

    @app.get("/diary", response_class=HTMLResponse)
    async def diary_page() -> str:
        payload = await _diary_payload()
        if not payload.get("enabled"):
            body = (
                "<p class='empty'>行为调度没启用（DSD_BEHAVIOR_ENABLED=false），"
                "所以还没有日记。</p>"
            )
        else:
            emotion = payload.get("emotion") or {}
            cards = "".join(
                f"<article class='card'><h2>{html.escape(item['day'])}</h2>"
                f"<p>{html.escape(item['content']).replace(chr(10), '<br>')}</p></article>"
                for item in payload.get("diaries") or []
            )
            if not cards:
                cards = (
                    "<p class='empty'>还没有日记。凌晨 03:30 会自动写一篇；也可以现在让我补写。</p>"
                )
            body = (
                f"<section class='status'>"
                f"<span>情绪：{html.escape(str(emotion.get('mood', '?')))}"
                f"（精力 {float(emotion.get('energy', 0)):.2f}）</span>"
                f"<span>今天主动消息：{payload.get('proactive_today', 0)} 条</span>"
                f"<span>当前：{'睡觉中' if payload.get('is_asleep') else '醒着'}</span>"
                f"</section>{cards}"
            )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>鲸鲸的日记</title>
<style>
  body {{ margin:0; padding:24px; background:#14161c; color:#e7e9ef;
         font:16px/1.75 -apple-system, "PingFang SC", system-ui, sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 6px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:20px; }}
  .status {{ display:flex; gap:16px; flex-wrap:wrap; color:#aab2c5; font-size:13px;
             padding:10px 14px; border:1px solid #2a2f3a; border-radius:10px; margin-bottom:20px; }}
  .card {{ background:#191c23; border:1px solid #2a2f3a; border-radius:12px;
           padding:16px 18px; margin-bottom:16px; }}
  .card h2 {{ font-size:15px; margin:0 0 10px; color:#9ecbff; font-weight:600; }}
  .card p {{ margin:0; white-space:pre-wrap; }}
  .empty {{ color:#8b93a7; }}
  .note {{ margin-top:24px; color:#6f7789; font-size:12px; }}
</style></head><body>
<h1>鲸鲸的日记</h1>
<div class="sub">她自己写的，第一人称；每 60 秒自动刷新。数据在 data/behavior.db</div>
{body}
<p class="note">日记只是内心记录，不会被当成"已发生的事实"；她聊天时可以自然引用。</p>
</body></html>"""

    @app.post("/api/phone/peek")
    async def trigger_peek() -> dict[str, Any]:
        if peeker is None:
            raise HTTPException(status_code=400, detail="主动看手机没启用")
        text = await peeker.peek_once(force=True)
        return {"sent": text or ""}

    @app.post("/api/phone/taobao")
    async def trigger_taobao() -> dict[str, Any]:
        if taobao is None:
            raise HTTPException(status_code=400, detail="淘宝逛街没启用")
        text = await taobao.browse_once(force=True)
        return {"sent": text or "", "reason": getattr(taobao, "last_skip", "")}

    app.include_router(router)
    if qq is not None:
        app.include_router(create_qq_router(qq))
        app.state.qq = qq
    if scheduler is not None:
        app.state.scheduler = scheduler
    app.state.mcp = mcp_manager

    app.state.agent = agent
    app.state.settings = settings
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("dsdaughter.main:app", host=settings.host, port=settings.port)
