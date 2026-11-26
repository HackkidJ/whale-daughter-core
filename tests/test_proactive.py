"""主动性：时段触发、配额、免打扰、调度器发送。"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dsdaughter.agent import Agent
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona
from dsdaughter.proactive import (
    InMemoryProactiveStore,
    ProactiveScheduler,
    ProactiveState,
    in_quiet_hours,
    pick_trigger,
)
from dsdaughter.types import Turn

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"
TZ = ZoneInfo("Asia/Shanghai")


def settings(**overrides) -> Settings:
    base = {
        "memory_extract": False,
        "tools_enabled": False,
        "qq_allowed_users": "123",
        "proactive_enabled": True,
        "proactive_tick_seconds": 5,
        "proactive_daily_limit": 14,
        "proactive_min_gap_minutes": 25,
        "proactive_miss_probability": 0.0,
        "proactive_miss_gap_minutes": 60,
        "proactive_quiet_hours": "00:30-08:00",
        "timezone": "Asia/Shanghai",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def at(hour: int, minute: int = 0, *, day: int = 20) -> datetime:
    """2026-11-20 是周五；day=21 是周六。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


# --------------------------------------------------------------------- 免打扰
def test_quiet_hours_normal_and_wrapping():
    assert in_quiet_hours(at(2), "00:30-08:00") is True
    assert in_quiet_hours(at(9), "00:30-08:00") is False
    assert in_quiet_hours(at(23, 30), "23:00-07:00") is True
    assert in_quiet_hours(at(6), "23:00-07:00") is True
    assert in_quiet_hours(at(12), "23:00-07:00") is False
    assert in_quiet_hours(at(12), "") is False


# --------------------------------------------------------------------- 触发
def test_workday_morning_window():
    trigger = pick_trigger(now=at(9, 30), state=ProactiveState(), settings=settings())
    assert trigger is not None
    assert trigger.kind == "morning_before_work"


def test_lunch_and_off_work_windows():
    assert pick_trigger(now=at(12, 10), state=ProactiveState(), settings=settings()).kind == "lunch"
    assert (
        pick_trigger(now=at(19, 0), state=ProactiveState(), settings=settings()).kind == "off_work"
    )


def test_goodnight_window():
    trigger = pick_trigger(now=at(22, 40), state=ProactiveState(), settings=settings())
    assert trigger is not None
    assert trigger.kind == "goodnight"


def test_weekend_morning_only_on_weekend():
    weekday = pick_trigger(now=at(10, 40), state=ProactiveState(), settings=settings())
    assert weekday is None
    weekend = pick_trigger(now=at(10, 40, day=21), state=ProactiveState(), settings=settings())
    assert weekend is not None
    assert weekend.kind == "weekend_morning"


def test_quiet_hours_block_everything():
    assert pick_trigger(now=at(3), state=ProactiveState(), settings=settings()) is None


def test_same_kind_not_repeated_today():
    state = ProactiveState(kinds_today={"morning_before_work"})
    assert pick_trigger(now=at(9, 30), state=state, settings=settings()) is None


def test_min_gap_blocks():
    state = ProactiveState(last_sent_at=at(9, 20))
    assert pick_trigger(now=at(9, 30), state=state, settings=settings()) is None


def test_daily_limit_blocks():
    state = ProactiveState(count_today=2, last_sent_at=at(9, 0))
    assert (
        pick_trigger(
            now=at(12, 10),
            state=state,
            settings=settings(proactive_daily_limit=2),
        )
        is None
    )


def test_random_miss_when_idle():
    state = ProactiveState(last_sent_at=at(14, 0))
    trigger = pick_trigger(
        now=at(16, 0),
        state=state,
        settings=settings(proactive_miss_probability=1.0, proactive_miss_gap_minutes=60),
        rng=random.Random(0),
    )
    assert trigger is not None
    assert trigger.kind == "miss"


def test_disabled_means_no_trigger():
    assert (
        pick_trigger(
            now=at(9, 30), state=ProactiveState(), settings=settings(proactive_enabled=False)
        )
        is None
    )


# --------------------------------------------------------------------- 状态存储
async def test_inmemory_store_state_and_record():
    store = InMemoryProactiveStore()
    since = at(0).astimezone(UTC)
    await store.record("s", "123", "morning_before_work", "早")
    state = await store.state("s", since)
    assert state.count_today == 1
    assert state.kinds_today == {"morning_before_work"}
    assert state.last_sent_at is not None
    await store.aclose()


# --------------------------------------------------------------------- 调度器
class FakeChannel:
    def __init__(self) -> None:
        self.server = SimpleNamespace(connected=True)
        self.sent: list[tuple[int | None, str]] = []

    async def send_text(self, *, text: str, user_id: int | None = None, group_id=None) -> int:
        self.sent.append((user_id, text))
        return 1


def make_agent(**overrides) -> Agent:
    return Agent(
        settings=settings(**overrides),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=10),
        llm=FakeLLM(reply="在忙吗？"),
    )


async def test_scheduler_sends_once_then_respects_gap():
    agent = make_agent()
    channel = FakeChannel()
    schedule = ProactiveScheduler(
        agent=agent, channel=channel, settings=agent.settings, store=InMemoryProactiveStore()
    )
    first = await schedule.tick(now=at(9, 30))
    assert first is not None and first.kind == "morning_before_work"
    assert channel.sent == [(123, "在忙吗？")]

    second = await schedule.tick(now=at(9, 35))
    assert second is None
    assert len(channel.sent) == 1
    await schedule.aclose()


async def test_scheduler_skips_when_disconnected():
    agent = make_agent()
    channel = FakeChannel()
    channel.server.connected = False
    schedule = ProactiveScheduler(
        agent=agent, channel=channel, settings=agent.settings, store=InMemoryProactiveStore()
    )
    assert await schedule.tick(now=at(9, 30)) is None
    assert channel.sent == []
    await schedule.aclose()


async def test_scheduler_skips_when_just_chatted():
    agent = make_agent()
    now = at(9, 30)
    await agent.memory.append_turns(
        "qq:u:123", [Turn("user", "刚聊过", now - timedelta(minutes=2))]
    )
    channel = FakeChannel()
    schedule = ProactiveScheduler(
        agent=agent, channel=channel, settings=agent.settings, store=InMemoryProactiveStore()
    )
    assert await schedule.tick(now=now) is None
    assert channel.sent == []
    await schedule.aclose()


async def test_scheduler_records_and_persists():
    agent = make_agent()
    channel = FakeChannel()
    store = InMemoryProactiveStore()
    schedule = ProactiveScheduler(
        agent=agent, channel=channel, settings=agent.settings, store=store
    )
    await schedule.tick(now=at(12, 10))
    state = await store.state("qq:u:123", at(0).astimezone(UTC))
    assert state.count_today == 1
    assert state.kinds_today == {"lunch"}
    await schedule.aclose()


async def test_scheduler_without_targets_does_nothing():
    agent = make_agent(qq_allowed_users="")
    channel = FakeChannel()
    schedule = ProactiveScheduler(
        agent=agent, channel=channel, settings=agent.settings, store=InMemoryProactiveStore()
    )
    assert await schedule.tick(now=at(9, 30)) is None
    await schedule.aclose()
