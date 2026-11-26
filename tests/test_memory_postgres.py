"""Postgres + pgvector 记忆集成测试。

默认跳过；需要真实库：
    DSD_TEST_DATABASE_URL=postgresql://dsd:change-me@127.0.0.1:5433/dsd \
        uv run pytest tests/test_memory_postgres.py -q
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dsdaughter.memory import MemoryItem, PostgresMemoryStore, RelationItem
from dsdaughter.proactive import PostgresProactiveStore
from dsdaughter.types import Turn

DSN = os.getenv("DSD_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="需要 DSD_TEST_DATABASE_URL 指向真实 Postgres")


def _turn(role: str, content: str) -> Turn:
    return Turn(role=role, content=content, ts=datetime.now(UTC))


@pytest.fixture
async def store():
    store = PostgresMemoryStore(DSN, max_turns=10)
    yield store
    await store.aclose()


def session_id() -> str:
    return f"it-{uuid.uuid4().hex[:10]}"


async def test_schema_and_history(store):
    session = session_id()
    await store.append_turns(session, [_turn("user", "你好"), _turn("assistant", "在呢")])
    history = await store.history(session)
    assert [item.content for item in history] == ["你好", "在呢"]
    assert [item.content for item in await store.history(session, limit=1)] == ["在呢"]
    await store.clear(session)


async def test_memories_recall_and_persistence(store):
    session = session_id()
    await store.add_memories(
        session,
        [
            MemoryItem("老爸喜欢喝不加糖的美式", kind="preference", keywords="咖啡 美式"),
            MemoryItem("今天天气不错", importance=0.3),
        ],
    )
    recalled = await store.recall(session, "老爸喝咖啡的口味", limit=3)
    assert recalled and recalled[0].content.startswith("老爸喜欢")

    # 换一个连接（模拟重启）还能读到 —— 这就是 P2a 的意义
    other = PostgresMemoryStore(DSN, max_turns=10)
    try:
        history_before = await store.history(session)
        assert history_before == []  # 这条 session 没写对话
        fresh = await other.recall(session, "咖啡", limit=3)
        assert fresh and fresh[0].content.startswith("老爸喜欢")
    finally:
        await other.aclose()
        await store.clear(session)


async def test_relations_episodes_and_clear(store):
    session = session_id()
    await store.upsert_relations(session, [RelationItem("称呼", "老爸")])
    await store.upsert_relations(session, [RelationItem("称呼", "爸爸")])
    await store.add_episode(session, "聊了咖啡口味。")
    await store.add_episode(session, "又聊了一次。")

    assert await store.profile(session) == {"称呼": "爸爸"}
    episodes = await store.episodes(session, limit=3)
    assert episodes[-1] == "又聊了一次。"

    await store.clear(session)
    assert await store.history(session) == []
    assert await store.recall(session, "咖啡") == []
    assert await store.profile(session) == {}


async def test_proactive_store_roundtrip():
    session = session_id()
    store = PostgresProactiveStore(DSN)
    try:
        since = datetime.now(UTC) - timedelta(days=1)
        await store.record(session, "123", "off_work", "下班啦")
        state = await store.state(session, since)
        assert state.count_today == 1
        assert state.kinds_today == {"off_work"}
        assert state.last_sent_at is not None
    finally:
        await store.aclose()
