"""记忆存储接口与进程内实现。"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Protocol

from ..types import Turn
from .base import MemoryItem, RecalledMemory, RelationItem
from .scoring import rank_memories


class MemoryStore(Protocol):
    """长期记忆接口。所有实现都必须是异步的（Postgres 版要查库）。"""

    async def append_turns(self, session_id: str, turns: list[Turn]) -> None: ...

    async def history(self, session_id: str, limit: int | None = None) -> list[Turn]: ...

    async def recall(self, session_id: str, query: str, limit: int = 6) -> list[RecalledMemory]: ...

    async def add_memories(self, session_id: str, items: list[MemoryItem]) -> int: ...

    async def upsert_relations(self, session_id: str, items: list[RelationItem]) -> None: ...

    async def add_episode(self, session_id: str, summary: str) -> None: ...

    async def profile(self, session_id: str) -> dict[str, str]:
        bucket = self._memories.get(session_id)
        return dict(bucket.relations) if bucket else {}

    async def episodes(self, session_id: str, limit: int = 3) -> list[str]:
        bucket = self._memories.get(session_id)
        if not bucket:
            return []
        return list(bucket.episodes.values())[-limit:]

    async def clear(self, session_id: str) -> None: ...

    async def aclose(self) -> None: ...


class SessionMemories:
    def __init__(self) -> None:
        self.items: list[tuple[MemoryItem, datetime]] = []
        self.relations: dict[str, str] = {}
        self.episodes: dict[str, str] = {}


class InMemoryMemoryStore:
    """进程内实现：开发/测试够用，重启就忘（这正是 P2a 要解决的问题）。"""

    def __init__(self, max_turns: int = 20) -> None:
        self._max_turns = max(1, max_turns)
        self._turns: dict[str, deque[Turn]] = {}
        self._memories: dict[str, SessionMemories] = defaultdict(SessionMemories)

    async def append_turns(self, session_id: str, turns: list[Turn]) -> None:
        bucket = self._turns.setdefault(session_id, deque(maxlen=self._max_turns))
        bucket.extend(turns)

    async def history(self, session_id: str, limit: int | None = None) -> list[Turn]:
        turns = list(self._turns.get(session_id, ()))
        return turns[-limit:] if limit else turns

    async def turns_between(self, session_id: str, start_ts: float, end_ts: float) -> list[Turn]:
        turns = self._turns.get(session_id, [])
        result: list[Turn] = []
        for turn in turns:
            ts = getattr(turn, "ts", None)
            if ts is None:
                continue
            value = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
            if start_ts <= value < end_ts:
                result.append(turn)
        return result

    async def recall(self, session_id: str, query: str, limit: int = 6) -> list[RecalledMemory]:
        bucket = self._memories.get(session_id)
        if not bucket or not bucket.items:
            return []
        candidates = [
            RecalledMemory(
                content=item.content,
                kind=item.kind,
                importance=item.importance,
                created_at=created_at,
                extra={"keywords": item.keywords},
            )
            for item, created_at in bucket.items
        ]
        return rank_memories(query=query, candidates=candidates, now=datetime.now(UTC), limit=limit)

    async def add_memories(self, session_id: str, items: list[MemoryItem]) -> int:
        bucket = self._memories[session_id]
        now = datetime.now(UTC)
        bucket.items.extend((item.normalized(), now) for item in items)
        bucket.items = bucket.items[-500:]
        return len(items)

    async def upsert_relations(self, session_id: str, items: list[RelationItem]) -> None:
        bucket = self._memories[session_id]
        for item in items:
            key, value = item.key.strip()[:64], item.value.strip()[:200]
            if key and value:
                bucket.relations[key] = value

    async def add_episode(self, session_id: str, summary: str) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        self._memories[session_id].episodes[day] = summary.strip()[:500]

    async def profile(self, session_id: str) -> dict[str, str]:
        bucket = self._memories.get(session_id)
        return dict(bucket.relations) if bucket else {}

    async def episodes(self, session_id: str, limit: int = 3) -> list[str]:
        bucket = self._memories.get(session_id)
        if not bucket:
            return []
        return list(bucket.episodes.values())[-limit:]

    async def clear(self, session_id: str) -> None:
        self._turns.pop(session_id, None)
        self._memories.pop(session_id, None)

    async def aclose(self) -> None:
        return None
