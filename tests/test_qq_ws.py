"""OneBot 反向 WS 集成测试：用 TestClient 扮演 NapCat。

验证：token 鉴权、心跳、私聊事件 → send_private_msg 动作 → echo 回包 → 统计。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dsdaughter.agent import Agent
from dsdaughter.channels.qq import QQChannel, create_router
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"
TOKEN = "test-token"


def make_app(reply: str = "在呢。") -> tuple[FastAPI, QQChannel]:
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        qq_token=TOKEN,
        qq_allowed_users="123",
        qq_allowed_groups="",
        qq_send_interval=0.0,
        qq_human_delay=False,
        qq_typing_status=False,
        data_dir=tempfile.mkdtemp(prefix="dsd-ws-test-"),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=6),
        llm=FakeLLM(reply=reply),
    )
    channel = QQChannel(agent=agent, settings=settings)
    app = FastAPI()
    app.include_router(create_router(channel))
    return app, channel


def heartbeat(self_id: int = 999) -> dict:
    return {
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "self_id": self_id,
        "status": {},
        "interval": 30000,
    }


def private_event(**overrides) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1001,
        "self_id": 999,
        "user_id": 123,
        "sender": {"nickname": "老爸"},
        "message": [{"type": "text", "data": {"text": "在吗"}}],
    }
    payload.update(overrides)
    return payload


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


def test_private_message_roundtrip():
    app, channel = make_app(reply="在的呀。")
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/onebot/v11/ws?access_token={TOKEN}") as ws,
    ):
        ws.send_json(heartbeat())
        ws.send_json(private_event())

        action = ws.receive_json()
        assert action["action"] == "send_private_msg"
        assert action["params"]["user_id"] == 123
        assert action["params"]["message"] == [{"type": "text", "data": {"text": "在的呀。"}}]
        assert action["echo"]

        ws.send_json(
            {"status": "ok", "retcode": 0, "data": {"message_id": 7}, "echo": action["echo"]}
        )
        _wait(lambda: channel.stats["replied"] == 1)

    assert channel.server.self_id == 999
    assert channel.stats["received"] == 1


def test_bad_token_is_rejected():
    app, _ = make_app()
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/onebot/v11/ws?access_token=wrong") as ws,
    ):
        ws.receive_json()


def test_authorization_header_token():
    app, channel = make_app()
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/onebot/v11/ws", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws,
    ):
        ws.send_json(heartbeat())
        _wait(lambda: channel.server.connected)
    assert channel.server.self_id == 999


def test_admin_send_requires_token():
    app, _ = make_app()
    with TestClient(app) as client:
        assert client.post("/api/qq/send", json={"text": "hi", "user_id": 123}).status_code == 401
        assert (
            client.post(
                "/api/qq/send",
                json={"text": "hi", "user_id": 123},
                headers={"X-Admin-Token": "wrong"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/qq/send",
                json={"text": "hi", "user_id": 123},
                headers={"X-Admin-Token": TOKEN},
            ).status_code
            == 502  # 没连 NapCat，OneBotError → 502
        )


def test_admin_send_validates_target():
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/qq/send",
            json={"text": "hi", "user_id": 123, "group_id": 456},
            headers={"X-Admin-Token": TOKEN},
        )
    assert response.status_code == 400


def test_avatar_endpoint_requires_token_and_image():
    app, _ = make_app()
    with TestClient(app) as client:
        assert client.post("/api/qq/avatar", content=b"x").status_code == 401
        response = client.post(
            "/api/qq/avatar",
            content=b"not-an-image",
            headers={"X-Admin-Token": TOKEN},
        )
        assert response.status_code == 415
        response = client.post(
            "/api/qq/avatar",
            content=b"not-an-image",
            headers={"X-Admin-Token": TOKEN, "Content-Type": "image/jpeg"},
        )
        assert response.status_code == 502  # 没连 NapCat


def test_status_endpoint():
    app, _ = make_app()
    with TestClient(app) as client:
        response = client.get("/api/qq/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["allowed_users"] == [123]
    assert body["persona"] == Persona.load(PERSONA_PATH).name
    assert body["requests"] == 0


def test_healthz_includes_qq_when_enabled():
    from dsdaughter.main import create_app

    settings = Settings(
        _env_file=None,
        memory_extract=False,
        llm_mode="fake",
        qq_enabled=True,
        qq_token="",
        qq_allowed_users="123",
        qq_human_delay=False,
        qq_typing_status=False,
        data_dir=tempfile.mkdtemp(prefix="dsd-ws-test-"),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯。"),
    )
    app = create_app(settings=settings, agent=agent)
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["qq"]["connected"] is False
    assert body["qq"]["allowed_users"] == [123]
