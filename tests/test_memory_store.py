"""进程内记忆 + agent 记忆注入/抽取链路。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from dsdaughter.agent import Agent
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import (
    ExtractionResult,
    InMemoryMemoryStore,
    MemoryItem,
    RelationItem,
)
from dsdaughter.persona import Persona
from dsdaughter.types import Turn

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"


def turn(role: str, content: str) -> Turn:
    return Turn(role=role, content=content, ts=datetime.now(UTC))


async def test_append_and_history_order():
    store = InMemoryMemoryStore(max_turns=3)
    await store.append_turns("s", [turn("user", "1"), turn("assistant", "2")])
    await store.append_turns("s", [turn("user", "3"), turn("assistant", "4")])

    history = await store.history("s")
    assert [item.content for item in history] == ["2", "3", "4"]
    assert [item.content for item in await store.history("s", limit=2)] == ["3", "4"]


async def test_recall_returns_relevant_first():
    store = InMemoryMemoryStore()
    await store.add_memories(
        "s",
        [
            MemoryItem("今天天气不错", importance=0.9),
            MemoryItem("老爸喜欢喝不加糖的美式", kind="preference", keywords="咖啡"),
        ],
    )
    recalled = await store.recall("s", "老爸喝咖啡喜欢什么", limit=3)
    assert recalled[0].content.startswith("老爸喜欢")


async def test_relations_and_episodes():
    store = InMemoryMemoryStore()
    await store.upsert_relations("s", [RelationItem("称呼", "老爸")])
    await store.upsert_relations("s", [RelationItem("称呼", "爸爸")])
    await store.add_episode("s", "聊了咖啡。")
    assert await store.profile("s") == {"称呼": "爸爸"}
    assert await store.episodes("s") == ["聊了咖啡。"]


async def test_clear_wipes_everything():
    store = InMemoryMemoryStore()
    await store.append_turns("s", [turn("user", "hi")])
    await store.add_memories("s", [MemoryItem("x")])
    await store.clear("s")
    assert await store.history("s") == []
    assert await store.recall("s", "x") == []


class StubExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, turns):
        self.calls += 1
        return ExtractionResult(
            memories=[MemoryItem("老爸怕冷，冬天手脚冰凉", importance=0.8)],
            relations=[RelationItem("称呼", "老爸")],
            episode="聊了怕冷这件事。",
        )


def make_agent(*, memory, extractor=None) -> Agent:
    return Agent(
        settings=Settings(
            _env_file=None, memory_extract=extractor is not None, tools_enabled=False
        ),
        persona=Persona.load(PERSONA_PATH),
        memory=memory,
        llm=FakeLLM(reply="记住啦。"),
        extractor=extractor,
    )


async def test_agent_injects_recalled_memory_into_prompt():
    store = InMemoryMemoryStore()
    await store.add_memories("s", [MemoryItem("老爸喜欢喝不加糖的美式", kind="preference")])
    await store.upsert_relations("s", [RelationItem("称呼", "老爸")])
    await store.add_episode("s", "昨天聊了咖啡。")

    agent = make_agent(memory=store)
    messages = await agent.build_messages("s", "给我推荐点喝的")
    system = messages[0].content
    assert "老爸喜欢喝不加糖的美式" in system
    assert "称呼：老爸" in system
    assert "昨天聊了咖啡" in system


async def test_agent_persists_and_extracts_in_background():
    store = InMemoryMemoryStore()
    extractor = StubExtractor()
    agent = make_agent(memory=store, extractor=extractor)

    await agent.reply("s", "我今天手好冷")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if extractor.calls:
            break

    assert extractor.calls == 1
    assert (await store.profile("s"))["称呼"] == "老爸"
    recalled = await store.recall("s", "手冷", limit=3)
    assert any("怕冷" in item.content for item in recalled)


async def test_agent_aclose_is_safe():
    store = InMemoryMemoryStore()
    agent = make_agent(memory=store)
    await agent.aclose()
