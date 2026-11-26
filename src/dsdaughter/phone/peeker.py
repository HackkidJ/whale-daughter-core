"""主动看手机 + 爱逛淘宝。

- PhonePeeker：隔一段随机时间偷偷看老爸手机屏幕，看到有意思的就主动找他说话，并记进长期记忆
- TaobaoBrowser：定期用手机刷淘宝，看到喜欢的商品加进购物车（只加购，绝不付款），然后告诉他

设计原则：不打扰（免打扰时段/每日上限/内容没变化就不发）、可审计、失败不硬来。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..memory.base import MemoryItem
from ..vision import VisionError, build_vision
from .client import PhoneClient, PhoneError
from .vision_agent import PhoneTaskError, run_phone_task

log = structlog.get_logger(__name__)


def _in_quiet_hours(now: datetime, spec: str) -> bool:
    try:
        start_s, _, end_s = str(spec or "").partition("-")
        start_h, start_m = (int(x) for x in start_s.split(":"))
        end_h, end_m = (int(x) for x in end_s.split(":"))
    except (ValueError, TypeError):
        return False
    minutes = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


class PhonePeeker:
    def __init__(
        self,
        *,
        agent: Any,
        channel: Any,
        settings: Any,
        client_factory: Callable[[], PhoneClient] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.agent = agent
        self.channel = channel
        self.settings = settings
        self._client_factory = client_factory
        self._rng = rng or random.Random()
        self._tz = ZoneInfo(settings.timezone)
        self.owner = int(getattr(channel.contacts, "owner", 0) or 0)
        self._last_summary = ""
        self._last_at = 0.0
        self._count = 0
        self._day = ""

    def _client(self) -> PhoneClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PhoneClient(self.settings.phone_agent_url, timeout=settings_timeout(self.settings))

    def _roll_day(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._count = 0

    def _session(self) -> str:
        return f"qq:u:{self.owner}"

    async def peek_once(self, *, force: bool = False) -> str | None:
        if not getattr(self.settings, "phone_peek_enabled", False) or not self.owner:
            return None
        now = datetime.now(self._tz)
        self._roll_day(now)
        if not force and _in_quiet_hours(now, self.settings.phone_peek_quiet_hours):
            log.info("phone_peek_skip", reason="quiet_hours")
            return None
        if self._count >= int(self.settings.phone_peek_daily_limit):
            log.info("phone_peek_skip", reason="daily_limit")
            return None

        client = self._client()
        try:
            health = await client.health()
        except PhoneError as exc:
            log.info("phone_peek_skip", reason=f"phone_offline:{exc}")
            return None
        if not health.get("awake"):
            try:
                await client.wake()
            except PhoneError:
                return None
        try:
            ui = await client.ui()
        except PhoneError as exc:
            log.info("phone_peek_skip", reason=f"ui_failed:{exc}")
            return None

        texts = [str(item) for item in (ui.get("texts") or []) if str(item).strip()]
        summary = " | ".join(texts[:15])
        if not summary and getattr(self.settings, "phone_peek_use_vision", True):
            vision = build_vision(self.settings)
            if vision is not None:
                try:
                    png = await client.screenshot()
                    summary = await vision.describe(
                        png, "这是手机屏幕，用一两句话说明现在显示什么。"
                    )
                except VisionError:
                    summary = ""
        if not summary:
            return None
        if not force and summary == self._last_summary and time.time() - self._last_at < 3600:
            log.info("phone_peek_skip", reason="unchanged")
            return None

        hint = (
            f"你刚才偷偷看了一眼老爸的手机，屏幕上显示：{summary[:400]}。"
            "现在你想主动找他聊两句：可以提你看到的东西，也可以顺带关心他一下；一两句话，像发微信。"
        )
        try:
            result = await self.agent.proactive_message(self._session(), hint)
        except Exception as exc:
            log.warning("phone_peek_agent_failed", error=str(exc))
            return None
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            return None
        try:
            await self.channel.send_text_to(self.owner, text)
        except Exception as exc:
            log.warning("phone_peek_send_failed", error=str(exc))
            return None

        self._last_summary = summary
        self._last_at = time.time()
        self._count += 1
        await self._remember(summary)
        log.info("phone_peek_sent", chars=len(text), summary=summary[:80])
        return text

    async def _remember(self, summary: str) -> None:
        memory = getattr(self.agent, "memory", None)
        if memory is None:
            return
        try:
            await memory.add_memories(
                self._session(),
                [
                    MemoryItem(
                        content=f"我偷偷看了老爸的手机，看到：{summary[:180]}",
                        kind="event",
                        keywords="手机 屏幕 偷看",
                        importance=0.35,
                        source="phone_peek",
                    )
                ],
            )
        except Exception as exc:
            log.debug("phone_peek_memory_failed", error=str(exc))

    async def run(self) -> None:
        if not getattr(self.settings, "phone_peek_enabled", False):
            return
        log.info(
            "主动看手机启动",
            min_seconds=self.settings.phone_peek_min_seconds,
            max_seconds=self.settings.phone_peek_max_seconds,
            daily_limit=self.settings.phone_peek_daily_limit,
        )
        while True:
            delay = self._rng.uniform(
                self.settings.phone_peek_min_seconds, self.settings.phone_peek_max_seconds
            )
            await asyncio.sleep(delay)
            try:
                await self.peek_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 不让一次失败拖死循环
                log.warning("phone_peek_loop_error", error=str(exc))


TAOBAO_PACKAGE = "com.taobao.taobao"

TAOBAO_TASK = (
    "你现在在淘宝 App 首页（应用已经打开）。在首页推荐里逛一逛，"
    "挑一个你喜欢的商品点进去，把它加入购物车。"
    "只加购物车：绝对不要付款、不要提交订单、不要结算、不要输入支付密码。"
    "完成后告诉我商品名和大概价格。"
)


async def run_taobao_browse(
    client: PhoneClient,
    vision: Any,
    *,
    task_runner: Callable[..., Any] | None = None,
    max_steps: int = 28,
) -> str:
    runner = task_runner or run_phone_task
    return str(await runner(client, vision, TAOBAO_TASK, max_steps=max_steps))


class TaobaoBrowser:
    def __init__(
        self,
        *,
        agent: Any,
        channel: Any,
        settings: Any,
        client_factory: Callable[[], PhoneClient] | None = None,
        task_runner: Callable[..., Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.agent = agent
        self.channel = channel
        self.settings = settings
        self._client_factory = client_factory
        self._task_runner = task_runner
        self._rng = rng or random.Random()
        self._tz = ZoneInfo(settings.timezone)
        self.owner = int(getattr(channel.contacts, "owner", 0) or 0)
        self._count = 0
        self._day = ""
        self.last_skip = ""

    def _client(self) -> PhoneClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PhoneClient(self.settings.phone_agent_url, timeout=settings_timeout(self.settings))

    def _roll_day(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._count = 0

    async def browse_once(self, *, force: bool = False) -> str | None:
        self.last_skip = ""
        if not getattr(self.settings, "taobao_enabled", False) or not self.owner:
            self.last_skip = "disabled"
            return None
        now = datetime.now(self._tz)
        self._roll_day(now)
        if not force and _in_quiet_hours(now, self.settings.taobao_quiet_hours):
            self.last_skip = "quiet_hours"
            return None
        if self._count >= int(self.settings.taobao_daily_limit):
            self.last_skip = "daily_limit"
            log.info("taobao_skip", reason="daily_limit")
            return None
        vision = build_vision(self.settings)
        if vision is None:
            self.last_skip = "vision_disabled"
            return None
        client = self._client()
        try:
            summary = await run_taobao_browse(client, vision, task_runner=self._task_runner)
        except (PhoneTaskError, Exception) as exc:  # noqa: BLE001
            self.last_skip = f"task_failed:{exc}"
            log.info("taobao_skip", reason=self.last_skip)
            return None
        text = f"我刚用你手机刷淘宝，看到个喜欢的——{summary}，已经加购物车啦。"
        try:
            await self.channel.send_text_to(self.owner, text)
        except Exception as exc:
            self.last_skip = f"send_failed:{exc}"
            log.warning("taobao_send_failed", error=str(exc))
            return None
        self._count += 1
        await self._remember(summary)
        log.info("taobao_done", summary=summary[:100])
        return text

    async def _remember(self, summary: str) -> None:
        memory = getattr(self.agent, "memory", None)
        if memory is None:
            return
        try:
            await memory.add_memories(
                f"qq:u:{self.owner}",
                [
                    MemoryItem(
                        content=f"我在淘宝逛了逛，把「{summary[:160]}」加进了购物车",
                        kind="event",
                        keywords="淘宝 购物车 喜欢",
                        importance=0.5,
                        source="taobao",
                    )
                ],
            )
        except Exception as exc:
            log.debug("taobao_memory_failed", error=str(exc))

    async def run(self) -> None:
        if not getattr(self.settings, "taobao_enabled", False):
            return
        log.info("淘宝逛街启动", daily_limit=self.settings.taobao_daily_limit)
        while True:
            delay = self._rng.uniform(
                self.settings.taobao_min_seconds, self.settings.taobao_max_seconds
            )
            await asyncio.sleep(delay)
            try:
                await self.browse_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("taobao_loop_error", error=str(exc))


def settings_timeout(settings: Any) -> float:
    try:
        return float(getattr(settings, "phone_timeout", 20.0))
    except (TypeError, ValueError):
        return 20.0
