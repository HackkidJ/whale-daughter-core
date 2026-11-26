"""拟人化调度 + 防幻觉（设备状态/回执/出站校验）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from dsdaughter.behavior.clock import BioClock
from dsdaughter.behavior.context import BehaviorContext
from dsdaughter.behavior.diary import DiaryWriter
from dsdaughter.behavior.store import BehaviorStore
from dsdaughter.claim_guard import ClaimGuard, ExecutionLedger
from dsdaughter.config import Settings
from dsdaughter.device_status import DeviceSnapshot, DeviceStatus
from dsdaughter.phone.client import PhoneError

TZ = ZoneInfo("Asia/Shanghai")


def settings(**overrides) -> Settings:
    base = {
        "timezone": "Asia/Shanghai",
        "phone_enabled": True,
        "phone_agent_url": "http://127.0.0.1:8765",
        "behavior_enabled": True,
        "claim_guard_enabled": True,
        "device_status_enabled": True,
        "memory_extract": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def dt(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


# --------------------------------------------------------------------- 生物钟
def test_sleep_window_weekday_and_weekend():
    clock = BioClock(settings())
    assert clock.is_asleep(dt(11, 24, 1, 0)) is True  # 周二凌晨
    assert clock.is_asleep(dt(11, 24, 12, 0)) is False
    assert clock.is_asleep(dt(11, 28, 8, 0)) is True  # 周六 08:00 还在周末睡眠
    assert clock.is_asleep(dt(11, 29, 10, 0)) is False  # 周日 10:00 已醒


def test_goodnight_window_and_insomnia():
    clock = BioClock(settings())
    assert clock.in_goodnight_window(dt(11, 23, 23, 30))
    assert clock.in_goodnight_window(dt(11, 24, 1, 30))
    assert not clock.in_goodnight_window(dt(11, 24, 3, 0))
    assert clock.insomnia(dt(11, 24, 2, 30))
    assert not clock.insomnia(dt(11, 24, 1, 30))


def test_energy_and_mood():
    clock = BioClock(settings())
    assert 0.0 <= clock.energy(dt(11, 24, 3, 0)) <= 0.25
    assert clock.energy(dt(11, 24, 9, 0)) > clock.energy(dt(11, 24, 3, 0))
    assert clock.mood_from_energy(0.1, dt(11, 24, 3, 0)) == "困倦"


# --------------------------------------------------------------------- 存储
async def test_behavior_store_roundtrip(tmp_path):
    store = BehaviorStore(tmp_path / "behavior.db")
    await store.set_state(last_user_at=123.0)
    assert float(await store.get_value("last_user_at")) == 123.0

    await store.set_emotion("低落", 0.2, "测试")
    emotion = await store.get_emotion()
    assert emotion.mood == "低落" and emotion.energy == pytest.approx(0.2)

    await store.add_diary("2026-11-23", "今天有点想他。")
    diaries = await store.recent_diaries(3)
    assert diaries[0].day == "2026-11-23"

    await store.log_proactive("miss", "想你了")
    assert await store.count_proactive_today() >= 1
    assert await store.last_proactive_at("miss") > 0


async def test_diary_writer(tmp_path):
    class FakeMemory:
        async def turns_between(self, session_id, start, end):
            from dsdaughter.types import Turn

            return [
                Turn("user", "今天好累", dt(11, 23, 20)),
                Turn("assistant", "那就早点睡", dt(11, 23, 20)),
            ] * 3

    class FakeLLM:
        async def stream(self, messages, **kwargs):
            from dsdaughter.types import ChatResult, LLMEvent

            result = ChatResult()
            result.text = "今天和他聊了会儿，感觉他挺累的。"
            yield LLMEvent("done", payload={"result": result})

    class FakeAgent:
        memory = FakeMemory()
        llm = FakeLLM()
        persona = type("P", (), {"name": "鱼鱼"})()

    store = BehaviorStore(tmp_path / "behavior.db")
    writer = DiaryWriter(FakeAgent(), store, settings())
    content = await writer.write_for(date(2026, 11, 23), "qq:u:1")
    assert content and "挺累" in content
    assert (await store.recent_diaries(1))[0].day == "2026-11-23"


async def test_behavior_context_injects_diary(tmp_path):
    store = BehaviorStore(tmp_path / "behavior.db")
    await store.set_emotion("开心", 0.9)
    await store.add_diary("2026-11-23", "昨天他夸我了，开心。")
    context = BehaviorContext(store, BioClock(settings()), settings())
    block = await context.prompt_block()
    assert "情绪" in block
    assert "2026-11-23" in block
    assert "不是刚刚发生的事实" in block


# --------------------------------------------------------------------- 设备状态
async def test_device_status_offline(monkeypatch):
    async def boom():
        raise PhoneError("没有连接")

    class Client:
        health = staticmethod(boom)

    status = DeviceStatus(settings(), client_factory=lambda: Client())
    snapshot = await status.probe(force=True)
    assert snapshot.connected is False
    block = status.prompt_block(snapshot)
    assert "未连接" in block
    assert "无法" in block


async def test_device_status_online_and_cache():
    calls = {"n": 0}

    class Client:
        async def health(self):
            calls["n"] += 1
            return {"ok": True, "battery": 80, "current_app": "com.tencent.mm"}

    status = DeviceStatus(settings(), client_factory=lambda: Client())
    snapshot = await status.probe(force=True)
    assert snapshot.connected and snapshot.battery == 80
    await status.probe()
    assert calls["n"] == 1  # 走缓存


# --------------------------------------------------------------------- 出站校验
class FakeAgent:
    def __init__(self, rewrite: str | None = None) -> None:
        self.rewrite_text = rewrite
        self.calls = 0

    async def rewrite(self, original, note, *, user_text=""):
        self.calls += 1
        return self.rewrite_text or "我这边连不上你的手机，没做成。"


async def test_claim_guard_blocks_fabricated_phone_action():
    agent = FakeAgent()
    guard = ClaimGuard(agent)
    ledger = ExecutionLedger()
    offline = DeviceSnapshot(connected=False, reason="ADB 断开")
    text = await guard.enforce("我把杯子加进购物车了！", ledger, offline, user_text="帮我逛淘宝")
    assert "连不上" in text
    assert agent.calls == 1


async def test_claim_guard_allows_with_receipt():
    agent = FakeAgent()
    guard = ClaimGuard(agent)
    ledger = ExecutionLedger()
    ledger.record("phone_taobao_browse", True)
    online = DeviceSnapshot(connected=True)
    text = await guard.enforce("我把杯子加进购物车了！", ledger, online)
    assert "购物车" in text
    assert agent.calls == 0


async def test_claim_guard_normal_reply_untouched():
    guard = ClaimGuard(FakeAgent())
    text = await guard.enforce(
        "今天好累呀，早点睡。", ExecutionLedger(), DeviceSnapshot(connected=False)
    )
    assert text == "今天好累呀，早点睡。"
