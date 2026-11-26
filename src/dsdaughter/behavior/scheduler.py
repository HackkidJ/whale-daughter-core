"""拟人化行为调度：生物钟、情绪、晚安、想念、失眠、日记。

设计约束：独立 asyncio 任务；每个任务异常隔离，绝不影响主对话。
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from .clock import BioClock
from .diary import DiaryWriter
from .store import BehaviorStore

log = structlog.get_logger(__name__)


class BehaviorScheduler:
    def __init__(
        self,
        *,
        agent,
        channel,
        settings,
        store: BehaviorStore,
        clock: BioClock,
        diary: DiaryWriter,
        rng: random.Random | None = None,
    ) -> None:
        self.agent = agent
        self.channel = channel
        self.settings = settings
        self.store = store
        self.clock = clock
        self.diary = diary
        self.tz = ZoneInfo(settings.timezone)
        self._rng = rng or random.Random()
        self.owner = int(getattr(channel.contacts, "owner", 0) or 0)
        self.session_id = f"qq:u:{self.owner}"

    # ------------------------------------------------------------------ 用户活动
    async def on_user_message(self) -> None:
        now = datetime.now(self.tz)
        await self.store.set_state(last_user_at=now.timestamp())
        if self.clock.is_asleep(now):
            delay = self._rng.uniform(
                float(self.settings.wake_reply_delay_min),
                float(self.settings.wake_reply_delay_max),
            )
            log.info("asleep_reply_delay", seconds=round(delay, 1))
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ 主循环
    async def run(self) -> None:
        if not self.settings.behavior_enabled:
            return
        log.info(
            "行为调度启动",
            tick_seconds=self.settings.behavior_tick_seconds,
            sleep=f"{self.settings.sleep_start}-{self.settings.sleep_end}",
            goodnight=f"{self.settings.goodnight_window_start}-{self.settings.goodnight_window_end}",
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("behavior_tick_failed", error=str(exc))
            await asyncio.sleep(float(self.settings.behavior_tick_seconds))

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.tz)
        await self._safe(self._update_emotion, now)
        if self.clock.in_goodnight_window(now):
            await self._safe(self._goodnight, now)
        if self.clock.insomnia(now):
            await self._safe(self._insomnia, now)
        await self._safe(self._miss, now)
        await self._safe(self._diary, now)

    async def _safe(self, job, now: datetime) -> None:
        try:
            await job(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("behavior_job_failed", job=getattr(job, "__name__", "?"), error=str(exc))

    # ------------------------------------------------------------------ 情绪
    async def _update_emotion(self, now: datetime) -> None:
        energy = self.clock.energy(now)
        mood = self.clock.mood_from_energy(energy, now)
        current = await self.store.get_emotion()
        if abs(current.energy - energy) < 0.08 and current.mood == mood:
            return
        await self.store.set_emotion(mood, energy, "生物钟")

    # ------------------------------------------------------------------ 主动消息
    async def _cap_reached(self) -> bool:
        count = await self.store.count_proactive_today()
        return count >= int(self.settings.behavior_proactive_daily_limit)

    async def _send(self, kind: str, hint: str, now: datetime) -> bool:
        if await self._cap_reached():
            return False
        result = await self.agent.proactive_message(self.session_id, hint)
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            return False
        await self.channel.send_text_to(self.owner, text)
        await self.store.log_proactive(kind, text)
        log.info("behavior_proactive_sent", kind=kind, chars=len(text))
        return True

    async def _goodnight(self, now: datetime) -> None:
        key = self._night_key(now)
        if await self.store.get_value("last_goodnight_night") == key:
            return
        hint = (
            f"已经 {now.strftime('%H:%M')} 了，到该睡觉的时间了。你想跟他说一句晚安，"
            "可以顺便提醒他别熬太晚。不要编造刚做过的事。"
        )
        if await self._send("goodnight", hint, now):
            await self.store.set_state(last_goodnight_night=key)

    async def _insomnia(self, now: datetime) -> None:
        last_user = float(await self.store.get_value("last_user_at", "0") or 0)
        if not last_user or now.timestamp() - last_user > 90 * 60:
            return
        key = self._night_key(now)
        if await self.store.get_value("last_insomnia_night") == key:
            return
        hint = (
            f"现在都 {now.strftime('%H:%M')} 了，他还没睡——他失眠了。"
            "你有点担心，也有点困，想问他一句怎么还不睡。不要编造做过的事。"
        )
        if await self._send("insomnia", hint, now):
            await self.store.set_state(last_insomnia_night=key)

    async def _miss(self, now: datetime) -> None:
        last_user = float(await self.store.get_value("last_user_at", "0") or 0)
        if not self.clock.miss_due(last_user, now):
            return
        last_miss = await self.store.last_proactive_at("miss")
        if now.timestamp() - last_miss < float(self.settings.miss_cooldown_hours) * 3600:
            return
        mood = (await self.store.get_emotion()).mood
        if mood in {"低落", "委屈"}:
            hint = "你好几个小时没跟他说话了，有点委屈、有点想他。想主动发一句。不要编造做过的事。"
        else:
            hint = "你好几个小时没跟他说话了，有点想他。想主动关心一句。不要编造做过的事。"
        await self._send("miss", hint, now)

    # ------------------------------------------------------------------ 日记
    async def _diary(self, now: datetime) -> None:
        if now.strftime("%H:%M") < self.settings.diary_at:
            return
        today = date.today()
        if await self.store.get_value("last_diary_day") == today.isoformat():
            return
        await self.diary.write_for(self.diary.yesterday(), self.session_id)

    def _night_key(self, now: datetime) -> str:
        return (now - timedelta(hours=12)).strftime("%Y-%m-%d")
