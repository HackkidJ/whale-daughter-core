"""HTTP 层：健康检查、普通聊天、SSE 流式、调试页。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from dsdaughter.agent import Agent
from dsdaughter.api import create_router
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"


def make_app(*, reply: str = "嗯。") -> FastAPI:
    agent = Agent(
        settings=Settings(
            _env_file=None,
            memory_extract=False,
            web_tools=True,
            history_turns=8,
            tools_enabled=False,
        ),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=8),
        llm=FakeLLM(reply=reply),
    )
    app = FastAPI()
    app.include_router(create_router(agent))
    return app


@asynccontextmanager
async def make_client(**kwargs: str):
    transport = httpx.ASGITransport(app=make_app(**kwargs))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_healthz():
    async with make_client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["persona"] == Persona.load(PERSONA_PATH).name
    assert body["web_tools"] is True


async def test_chat_returns_reply_and_citations():
    async with make_client(reply="我在呀。") as client:
        response = await client.post("/api/chat", json={"message": "在吗", "session_id": "t1"})
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "我在呀。"
    assert body["citations"][0]["url"].startswith("https://")


async def test_stream_endpoint_emits_sse_events():
    async with make_client(reply="好~") as client:
        response = await client.post("/api/chat/stream", json={"message": "在吗"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "event: text" in text
    assert "event: tool" in text  # fake provider 在 web=True 时会模拟一次搜索
    assert "event: done" in text
    assert "好~" in text


async def test_session_reset():
    async with make_client() as client:
        await client.post("/api/chat", json={"message": "记住我", "session_id": "s9"})
        response = await client.delete("/api/session/s9")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_index_page_served():
    persona_name = Persona.load(PERSONA_PATH).name
    async with make_client() as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert persona_name in response.text
    assert "__PERSONA_NAME__" not in response.text  # 占位符必须被替换掉
    assert "/api/chat/stream" in response.text


async def test_empty_message_rejected():
    async with make_client() as client:
        response = await client.post("/api/chat", json={"message": ""})
    assert response.status_code == 422


def test_diary_page_and_api(tmp_path, monkeypatch):
    import sqlite3
    import time

    from fastapi.testclient import TestClient

    from dsdaughter.config import Settings
    from dsdaughter.main import create_app

    db = tmp_path / "behavior.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS diary (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " day TEXT UNIQUE NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL);"
        "CREATE TABLE IF NOT EXISTS emotion (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " mood TEXT NOT NULL, energy REAL NOT NULL, reason TEXT, created_at REAL NOT NULL);"
        "CREATE TABLE IF NOT EXISTS proactive_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);"
    )
    conn.execute(
        "INSERT INTO diary(day, content, created_at) VALUES(?,?,?)",
        ("2026-11-24", "今天他夸我了，开心。", time.time()),
    )
    conn.execute(
        "INSERT INTO emotion(mood, energy, reason, created_at) VALUES(?,?,?,?)",
        ("开心", 0.9, "生物钟", time.time()),
    )
    conn.commit()
    conn.close()

    settings = Settings(
        _env_file=None,
        behavior_enabled=True,
        behavior_db_path=db,
        qq_enabled=True,
        proactive_enabled=False,
        plugins_enabled=False,
        memory_extract=False,
        data_dir=tmp_path,
        qq_allowed_users="1",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/diary")
        assert page.status_code == 200
        assert "今天他夸我了" in page.text
        assert "开心" in page.text
        api = client.get("/api/behavior/diary").json()
        assert api["enabled"] is True
        assert api["diaries"][0]["day"] == "2026-11-24"
