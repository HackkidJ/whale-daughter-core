"""人际关系：白名单持久化 + 她的"社交工具"。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsdaughter.channels.onebot import OneBotError
from dsdaughter.config import Settings
from dsdaughter.contacts import ContactStore
from dsdaughter.tools import ToolContext, ToolError, build_default_registry
from dsdaughter.tools import contacts as tools


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "data_dir": tmp_path,
        "owner_qq": "1000000001",
        "qq_allowed_users": "1000000001",
        "memory_extract": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeServer:
    connected = True
    self_id = 1000000002

    def __init__(
        self, unsupported: set[str] | None = None, friends: set[int] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.unsupported = unsupported or set()
        self.friends = friends or set()

    async def call(self, action: str, params: dict) -> dict:
        if action in self.unsupported:
            raise OneBotError(f"不支持的Api {action}")
        if action == "get_friend_list":
            data = [{"user_id": user_id, "nickname": f"f{user_id}"} for user_id in self.friends]
            return {"status": "ok", "retcode": 0, "data": data}
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {}}


class FakeChannel:
    def __init__(self, settings: Settings) -> None:
        self.contacts = ContactStore(settings)
        self.server = FakeServer(unsupported={"send_friend_request", "add_friend"})
        self.pending_requests: dict[str, dict] = {}
        self.sent: list[tuple[int, str]] = []

    async def send_text_to(self, user_id: int, text: str, *, note: str = "") -> int:
        self.sent.append((user_id, text))
        return 1


def make_ctx(tmp_path: Path) -> tuple[ToolContext, FakeChannel]:
    settings = make_settings(tmp_path)
    channel = FakeChannel(settings)
    return ToolContext(settings=settings, session_id="test", qq=channel), channel


# --------------------------------------------------------------------- 存储
def test_store_persists_and_protects_owner(tmp_path):
    settings = make_settings(tmp_path)
    store = ContactStore(settings)
    assert store.owner == 1000000001
    assert store.is_allowed(1000000001)

    store.allow(555, note="群友")
    store.remember_temp(777, 789)

    reloaded = ContactStore(settings)
    assert reloaded.is_allowed(555)
    assert reloaded.get(555).note == "群友"
    assert reloaded.temp_group(777) == 789

    with pytest.raises(ValueError):
        reloaded.disallow(1000000001)


def test_family_recipients_persist(tmp_path):
    settings = make_settings(tmp_path)
    store = ContactStore(settings)
    store.authorize_family_recipient("文件传输助手")
    reloaded = ContactStore(settings)
    assert reloaded.family_recipients == ["文件传输助手"]
    assert reloaded.is_family_recipient("文件传输助手")
    assert not reloaded.is_family_recipient("黄佳佳")


def test_contacts_file_shape(tmp_path):
    settings = make_settings(tmp_path)
    store = ContactStore(settings)
    store.allow(555)
    payload = json.loads((tmp_path / "contacts.json").read_text(encoding="utf-8"))
    assert payload["owner"] == 1000000001
    assert {item["user_id"] for item in payload["contacts"]} == {1000000001, 555}


# --------------------------------------------------------------------- 工具
async def test_list_contacts(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.contacts.allow(555, note="群友")
    result = await tools.list_contacts(ctx)
    assert result.ok
    assert "1000000001" in result.content
    assert "555" in result.content
    assert "群友" in result.content


async def test_allow_and_remove_contact(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    allowed = await tools.allow_contact(ctx, user_id=888, note="游戏搭子")
    assert allowed.ok
    assert channel.contacts.is_allowed(888)

    removed = await tools.remove_contact(ctx, user_id=888)
    assert removed.ok
    assert not channel.contacts.is_allowed(888)

    with pytest.raises(ToolError):
        await tools.remove_contact(ctx, user_id=1000000001)


async def test_delete_friend_calls_qq_and_removes_from_contacts(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.contacts.allow(999)
    channel.server.friends = {999}
    result = await tools.delete_friend(ctx, user_id=999, reason="太吵")

    assert result.ok
    assert channel.server.calls == [("delete_friend", {"user_id": 999})]
    assert not channel.contacts.is_allowed(999)


async def test_delete_friend_protects_owner(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        await tools.delete_friend(ctx, user_id=1000000001)


async def test_approve_friend_request(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.pending_requests["flag-1"] = {"user_id": 555, "comment": "我是群友"}
    result = await tools.approve_friend_request(ctx, user_id=555)

    assert result.ok
    assert channel.server.calls == [("set_friend_add_request", {"flag": "flag-1", "approve": True})]
    assert channel.contacts.is_allowed(555)
    assert channel.pending_requests == {}


async def test_approve_missing_request(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        await tools.approve_friend_request(ctx, user_id=404)


async def test_request_friend_reports_protocol_limit(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    result = await tools.request_friend(ctx, user_id=555, verify="你好")
    assert result.ok is False
    assert "主动加好友" in result.content


async def test_send_message_only_to_known_people(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.contacts.allow(555)
    ok = await tools.send_message(ctx, user_id=555, text="在吗")
    assert ok.ok
    assert channel.sent == [(555, "在吗")]

    with pytest.raises(ToolError):
        await tools.send_message(ctx, user_id=666, text="陌生人")


async def test_send_message_to_temp_session_uses_known_group(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.contacts.remember_temp(777, 789)
    result = await tools.send_message(ctx, user_id=777, text="又见面了")
    assert result.ok
    assert channel.sent == [(777, "又见面了")]


async def test_tools_registered_with_quota(tmp_path):
    settings = make_settings(tmp_path, tool_action_cooldown=0.0)
    registry = build_default_registry(settings)
    for name in (
        "list_contacts",
        "allow_contact",
        "remove_contact",
        "delete_friend",
        "approve_friend_request",
        "request_friend",
        "send_message",
    ):
        assert name in registry.names

    channel = FakeChannel(settings)
    ctx = ToolContext(settings=settings, session_id="test", qq=channel)
    for index in range(3):
        result = await registry.call("allow_contact", {"user_id": 1000 + index}, ctx)
        assert result.ok
    blocked = await registry.call("allow_contact", {"user_id": 2000}, ctx)
    assert blocked.ok is False
    assert "今天已经用过" in blocked.content


async def test_delete_friend_rejects_non_friend(tmp_path):
    ctx, channel = make_ctx(tmp_path)
    channel.server.friends = {123}
    with pytest.raises(ToolError):
        await tools.delete_friend(ctx, user_id=999)
    assert channel.server.calls == []  # 没真的发删除请求
