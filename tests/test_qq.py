"""QQ 通道逻辑：白名单、@ 触发、去重、拆条、会话隔离。"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest

from dsdaughter.agent import Agent
from dsdaughter.channels.onebot import MessageEvent
from dsdaughter.channels.qq import QQChannel
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"
SELF_ID = 999


class StubServer:
    """替代 OneBotServer，只记录发出去的消息和动作调用。"""

    def __init__(self) -> None:
        self.self_id = SELF_ID
        self.sent: list[tuple[str, int, list]] = []
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {}}

    def status(self) -> dict:
        return {"connected": True, "self_id": self.self_id}

    async def send_private(self, user_id: int, segments: list, *, group_id=None) -> dict:
        self.sent.append(("private", user_id, [s.to_dict() for s in segments]))
        self.last_group_id = group_id
        return {"status": "ok"}

    async def send_group(self, group_id: int, segments: list) -> dict:
        self.sent.append(("group", group_id, [s.to_dict() for s in segments]))
        return {"status": "ok"}


def make_channel(
    *,
    reply: str = "在呢。",
    users: str = "123",
    groups: str = "456",
    require_at: bool = True,
    max_chars: int = 900,
) -> QQChannel:
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        qq_allowed_users=users,
        qq_allowed_groups=groups,
        qq_group_require_at=require_at,
        qq_reply_max_chars=max_chars,
        qq_send_interval=0.0,
        qq_human_delay=False,
        qq_typing_status=False,
        qq_split_sentences=False,
        data_dir=tempfile.mkdtemp(prefix="dsd-qq-test-"),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=10),
        llm=FakeLLM(reply=reply),
    )
    channel = QQChannel(agent=agent, settings=settings)
    stub = StubServer()
    channel.server = stub  # type: ignore[assignment]
    return channel


def event(
    *,
    user_id: int = 123,
    group_id: int | None = None,
    message_id: int = 1,
    nickname: str = "老爸",
    segments: list[dict] | None = None,
    sub_type: str = "",
    message_type: str | None = None,
) -> MessageEvent:
    payload = {
        "post_type": "message",
        "message_type": message_type
        or (
            "private"
            if sub_type in {"group", "discuss"}
            else ("group" if group_id is not None else "private")
        ),
        "sub_type": sub_type,
        "message_id": message_id,
        "self_id": SELF_ID,
        "user_id": user_id,
        "sender": {"nickname": nickname},
        "message": segments or [{"type": "text", "data": {"text": "在吗"}}],
    }
    if group_id is not None:
        payload["group_id"] = group_id
    parsed = MessageEvent.from_payload(payload)
    assert parsed is not None
    return parsed


async def test_whitelisted_private_user_gets_reply():
    channel = make_channel(reply="在的呀。")
    await channel.handle_message(event())

    assert channel.server.sent == [
        ("private", 123, [{"type": "text", "data": {"text": "在的呀。"}}])
    ]
    assert channel.stats["replied"] == 1


async def test_stranger_is_ignored():
    channel = make_channel()
    await channel.handle_message(event(user_id=555))

    assert channel.server.sent == []
    assert channel.stats["ignored"] == 1


async def test_empty_whitelist_ignores_everyone():
    channel = make_channel(users="", groups="")
    await channel.handle_message(event())
    await channel.handle_message(event(group_id=456))

    assert channel.server.sent == []


async def test_group_requires_at():
    channel = make_channel()
    await channel.handle_message(event(group_id=456))
    assert channel.server.sent == []

    await channel.handle_message(
        event(
            group_id=456,
            message_id=2,
            segments=[
                {"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": " 在吗"}},
            ],
        )
    )
    assert len(channel.server.sent) == 1
    kind, target, segments = channel.server.sent[0]
    assert (kind, target) == ("group", 456)
    assert segments[0] == {"type": "at", "data": {"qq": "123"}}  # 回复时 @ 回去
    assert segments[1]["type"] == "text"


async def test_group_without_require_at():
    channel = make_channel(require_at=False)
    await channel.handle_message(event(group_id=456))
    assert len(channel.server.sent) == 1


async def test_non_whitelisted_group_is_ignored():
    channel = make_channel(groups="789")
    await channel.handle_message(
        event(
            group_id=456,
            segments=[
                {"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": " 在吗"}},
            ],
        )
    )
    assert channel.server.sent == []


async def test_duplicate_message_is_ignored():
    channel = make_channel()
    await channel.handle_message(event(message_id=42))
    await channel.handle_message(event(message_id=42))

    assert len(channel.server.sent) == 1
    assert channel.stats["ignored"] == 1


async def test_long_reply_is_split():
    channel = make_channel(reply="好" * 250, max_chars=100)
    await channel.handle_message(event())

    assert len(channel.server.sent) == 3
    assert all(len(segments[0]["data"]["text"]) <= 100 for _, _, segments in channel.server.sent)


async def test_empty_reply_sends_nothing():
    channel = make_channel(reply="   ")
    await channel.handle_message(event())
    assert channel.server.sent == []


async def test_session_ids_isolated_by_user_and_group():
    channel = make_channel()
    await channel.handle_message(event(user_id=123))
    await channel.handle_message(event(user_id=124, message_id=2))
    await channel.handle_message(
        event(
            group_id=456,
            message_id=3,
            segments=[
                {"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": " 在吗"}},
            ],
        )
    )

    # 124 不在白名单，不会产生会话；私聊和群聊各自独立
    assert len(await channel.agent.memory.history("qq:u:123")) == 2
    assert len(await channel.agent.memory.history("qq:g:456:u:123")) == 2
    assert await channel.agent.memory.history("qq:u:124") == []


async def test_own_message_is_ignored():
    channel = make_channel()
    await channel.handle_message(event(user_id=SELF_ID))
    assert channel.server.sent == []


@pytest.mark.parametrize("raw, expected", [("123, 456 ", {123, 456}), ("abc", set()), ("", set())])
def test_id_parsing(raw, expected):
    from dsdaughter.channels.qq import _parse_ids

    assert _parse_ids(raw) == expected


# ---------------------------------------------------------------- 好友申请
def friend_request(**overrides) -> dict:
    payload = {
        "post_type": "request",
        "request_type": "friend",
        "self_id": SELF_ID,
        "user_id": 123,
        "comment": "我是你爸",
        "flag": "flag-123",
    }
    payload.update(overrides)
    return payload


async def test_friend_request_from_whitelisted_user_is_auto_accepted():
    channel = make_channel()
    await channel.handle_request(friend_request())

    assert channel.server.calls == [
        ("set_friend_add_request", {"flag": "flag-123", "approve": True})
    ]
    assert channel.stats["requests"] == 1
    assert channel.pending_requests == {}


async def test_friend_request_from_stranger_is_not_accepted():
    channel = make_channel()
    await channel.handle_request(friend_request(user_id=555, flag="flag-555"))

    assert channel.server.calls == []
    assert "flag-555" in channel.pending_requests


async def test_friend_request_can_be_disabled():
    channel = make_channel()
    channel.settings = channel.settings.model_copy(update={"qq_auto_accept_friends": False})
    await channel.handle_request(friend_request())

    assert channel.server.calls == []
    assert "flag-123" in channel.pending_requests


async def test_non_friend_request_event_ignored():
    channel = make_channel()
    await channel.handle_request({"post_type": "request", "request_type": "group", "flag": "x"})
    assert channel.stats["requests"] == 0


# ---------------------------------------------------------------- 主动发送
async def test_send_text_private():
    channel = make_channel()
    count = await channel.send_text(text="在吗", user_id=123)

    assert count == 1
    assert channel.server.sent == [("private", 123, [{"type": "text", "data": {"text": "在吗"}}])]


async def test_send_text_group_and_requires_target():
    channel = make_channel()
    await channel.send_text(text="群消息", group_id=456)
    assert channel.server.sent[0][0] == "group"

    with pytest.raises(ValueError):
        await channel.send_text(text="没目标")


# ---------------------------------------------------------------- 头像
async def test_set_avatar_encodes_base64():
    channel = make_channel()
    image = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    await channel.set_avatar(image)

    action, params = channel.server.calls[0]
    assert action == "set_qq_avatar"
    assert params["file"].startswith("base64://")
    assert base64.b64decode(params["file"][len("base64://") :]) == image


async def test_set_avatar_rejects_empty():
    channel = make_channel()
    with pytest.raises(ValueError):
        await channel.set_avatar(b"")


# ---------------------------------------------------------------- 临时会话 & 动态白名单
async def test_temp_session_from_stranger_is_accepted():
    channel = make_channel(users="1000000001", groups="456")
    await channel.handle_message(
        event(user_id=555, nickname="路人", sub_type="group", group_id=789)
    )

    assert len(channel.server.sent) == 1
    kind, target, _ = channel.server.sent[0]
    assert (kind, target) == ("private", 555)
    assert channel.server.last_group_id == 789  # 回复走临时会话来源群
    assert channel.contacts.temp_group(555) == 789
    assert channel.stats["temp_received"] == 1
    assert await channel.agent.memory.history("qq:temp:555")  # 独立 session


async def test_plain_stranger_without_temp_session_is_ignored():
    channel = make_channel(users="1000000001", groups="456")
    await channel.handle_message(event(user_id=555, message_id=2))
    assert channel.server.sent == []
    assert channel.stats["ignored"] == 1


async def test_dynamic_whitelist_allow_and_remove():
    channel = make_channel(users="1000000001", groups="456")
    await channel.handle_message(event(user_id=555, message_id=3))
    assert channel.server.sent == []

    channel.contacts.allow(555, note="测试")
    await channel.handle_message(event(user_id=555, message_id=4))
    assert len(channel.server.sent) == 1

    channel.contacts.disallow(555)
    await channel.handle_message(event(user_id=555, message_id=5))
    assert len(channel.server.sent) == 1


async def test_owner_cannot_be_removed():
    channel = make_channel(users="1000000001", groups="456")
    assert channel.contacts.owner == 1000000001
    with pytest.raises(ValueError):
        channel.contacts.disallow(1000000001)


async def test_temp_session_can_be_disabled():
    channel = make_channel(users="1000000001", groups="456")
    channel.settings = channel.settings.model_copy(update={"qq_accept_temp_sessions": False})
    await channel.handle_message(event(user_id=555, sub_type="group", group_id=789, message_id=6))
    assert channel.server.sent == []


# ---------------------------------------------------------------- 语音回复
def make_voice_channel(tmp_path, *, probability: float = 1.0):
    settings = Settings(
        _env_file=None,
        qq_allowed_users="123",
        data_dir=tmp_path,
        qq_human_delay=False,
        qq_typing_status=False,
        qq_split_sentences=False,
        voice_enabled=True,
        voice_probability=probability,
        voice_max_chars=500,
        memory_extract=False,
        plugins_enabled=False,
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=6),
        llm=FakeLLM(reply="在呢。"),
    )
    channel = QQChannel(agent=agent, settings=settings)
    stub = StubServer()
    channel.server = stub  # type: ignore[assignment]
    return channel, stub


async def test_voice_reply_sends_record_segment(tmp_path, monkeypatch):
    from datetime import datetime

    from dsdaughter.media import FakeTTS

    channel, stub = make_voice_channel(tmp_path)
    # 深夜会降概率，固定成下午避免时间相关的抖动
    monkeypatch.setattr(channel.agent, "_now", lambda: datetime(2026, 1, 1, 15, 0))
    channel.tts = FakeTTS(0.2)
    await channel.handle_message(event(user_id=123))

    assert len(stub.sent) == 1
    segments = stub.sent[0][2]
    assert segments[0]["type"] == "record"
    assert segments[0]["data"]["file"].startswith("base64://")
    assert channel.stats["voice_replied"] == 1


async def test_voice_failure_falls_back_to_text(tmp_path):
    class BrokenTTS:
        async def synthesize(self, text):
            raise RuntimeError("tts down")

    channel, stub = make_voice_channel(tmp_path)
    channel.tts = BrokenTTS()
    await channel.handle_message(event(user_id=123))

    assert len(stub.sent) == 1
    assert stub.sent[0][2][0]["type"] == "text"
    assert channel.stats.get("voice_replied", 0) == 0


async def test_voice_probability_zero_keeps_text(tmp_path):
    from dsdaughter.media import FakeTTS

    channel, stub = make_voice_channel(tmp_path, probability=0.0)
    channel.tts = FakeTTS(0.2)
    await channel.handle_message(event(user_id=123))

    assert stub.sent[0][2][0]["type"] == "text"
