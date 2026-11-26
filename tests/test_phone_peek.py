"""主动看手机 / 淘宝逛街。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dsdaughter.config import Settings
from dsdaughter.phone.peeker import (
    PhonePeeker,
    TaobaoBrowser,
    _in_quiet_hours,
    run_taobao_browse,
)


def settings(**overrides) -> Settings:
    base = {
        "timezone": "Asia/Shanghai",
        "phone_enabled": True,
        "phone_peek_enabled": True,
        "phone_peek_min_seconds": 600,
        "phone_peek_max_seconds": 1200,
        "phone_peek_daily_limit": 3,
        "phone_peek_quiet_hours": "00:30-08:00",
        "taobao_enabled": True,
        "taobao_daily_limit": 2,
        "taobao_quiet_hours": "00:30-08:00",
        "memory_extract": False,
        "deepseek_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeClient:
    def __init__(self) -> None:
        self.sent_taps: list[tuple[int, int]] = []

    async def health(self) -> dict:
        return {"ok": True, "awake": True, "battery": 80, "current_app": "com.taobao.taobao"}

    async def open_app(self, package: str) -> dict:
        return {"ok": True, "package": package}

    async def ui(self) -> dict:
        return {"ok": True, "texts": ["淘宝", "猜你喜欢", "奶龙玩偶 19.9"], "summary": ""}

    async def wake(self) -> dict:
        return {"ok": True, "awake": True}

    async def screenshot(self) -> bytes:
        return b"png"


class FakeMemory:
    def __init__(self) -> None:
        self.items: list[tuple[str, list]] = []

    async def add_memories(self, session_id, items) -> int:
        self.items.append((session_id, list(items)))
        return len(items)


class FakeAgent:
    def __init__(self) -> None:
        self.memory = FakeMemory()
        self.hints: list[str] = []

    async def proactive_message(self, session_id: str, hint: str):
        self.hints.append(hint)
        return SimpleNamespace(text="诶——你在逛淘宝啊，看到什么好东西了？")


class FakeContacts:
    owner = 1001


class FakeChannel:
    def __init__(self) -> None:
        self.contacts = FakeContacts()
        self.sent: list[tuple[int, str]] = []
        self.agent = FakeAgent()

    async def send_text_to(self, user_id: int, text: str) -> int:
        self.sent.append((user_id, text))
        return 1


def make_peeker(**overrides):
    agent = FakeAgent()
    channel = FakeChannel()
    channel.agent = agent
    peeker = PhonePeeker(
        agent=agent,
        channel=channel,
        settings=settings(**overrides),
        client_factory=lambda: FakeClient(),
    )
    return peeker, agent, channel


def test_quiet_hours():
    tz = ZoneInfo("Asia/Shanghai")
    assert _in_quiet_hours(datetime(2026, 1, 1, 1, 0, tzinfo=tz), "00:30-08:00")
    assert _in_quiet_hours(datetime(2026, 1, 1, 23, 50, tzinfo=tz), "22:00-02:00")
    assert not _in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=tz), "00:30-08:00")


async def test_peek_sends_and_remembers():
    peeker, agent, channel = make_peeker()
    text = await peeker.peek_once(force=True)
    assert text
    assert channel.sent and channel.sent[0][0] == 1001
    assert agent.memory.items  # 记进长期记忆
    assert "奶龙" in agent.hints[0]


async def test_peek_skips_quiet_hours():
    peeker, _agent, channel = make_peeker(phone_peek_quiet_hours="00:00-23:59")
    assert await peeker.peek_once() is None
    assert channel.sent == []


async def test_peek_daily_limit():
    peeker, _agent, _channel = make_peeker(phone_peek_daily_limit=1)
    assert await peeker.peek_once(force=True)
    assert await peeker.peek_once(force=True) is None


async def test_peek_dedupes_unchanged_screen():
    peeker, _agent, channel = make_peeker()
    assert await peeker.peek_once(force=True)
    assert await peeker.peek_once() is None
    assert len(channel.sent) == 1


async def test_taobao_browse_once():
    agent = FakeAgent()
    channel = FakeChannel()
    channel.agent = agent

    async def fake_runner(client, vision, task, max_steps=28):
        assert "购物车" in task
        assert "不要付款" in task
        return "奶龙玩偶，大概 19.9 元"

    browser = TaobaoBrowser(
        agent=agent,
        channel=channel,
        settings=settings(),
        client_factory=lambda: FakeClient(),
        task_runner=fake_runner,
    )
    text = await browser.browse_once(force=True)
    assert text and "购物车" in text
    assert channel.sent
    assert agent.memory.items


async def test_run_taobao_browse_uses_task():
    async def fake_runner(client, vision, task, max_steps=28):
        assert "不要付款" in task
        return "测试商品"

    summary = await run_taobao_browse(FakeClient(), object(), task_runner=fake_runner)
    assert summary == "测试商品"
