"""Postgres + pgvector 长期记忆实现。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from importlib import resources

import asyncpg
import structlog

from ..types import Turn
from .base import MemoryItem, RecalledMemory, RelationItem
from .embedding import Embedder
from .scoring import rank_memories

log = structlog.get_logger(__name__)


def _parse_vector(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list):
        return None
    return [float(value) for value in values]


def _to_vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


class PostgresMemoryStore:
    def __init__(
        self,
        dsn: str,
        *,
        max_turns: int = 20,
        embedder: Embedder | None = None,
        dimension: int = 1024,
        half_life_days: float = 30.0,
        max_candidates: int = 300,
    ) -> None:
        self._dsn = dsn
        self._max_turns = max(1, max_turns)
        self._embedder = embedder
        self._dimension = max(1, int(dimension))
        self._half_life_days = half_life_days
        self._max_candidates = max_candidates
        self._pool: asyncpg.Pool | None = None
        self._init_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 连接
    async def _pool_or_init(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._init_lock:
            if self._pool is None:
                pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5, timeout=10)
                assert pool is not None
                await self._apply_schema(pool)
                self._pool = pool
                log.info("记忆库已连接")
        return self._pool

    async def _apply_schema(self, pool: asyncpg.Pool) -> None:
        sql = resources.files("dsdaughter.memory").joinpath("schema.sql").read_text("utf-8")
        sql = sql.replace("{dim}", str(self._dimension))
        async with pool.acquire() as connection:
            await connection.execute(sql)

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if self._embedder is None:
            return None
        try:
            return await self._embedder.embed(texts)
        except Exception as exc:  # 向量服务挂了不能影响主流程
            log.warning("embedding 失败，退回词面检索", error=str(exc))
            return None

    # ------------------------------------------------------------------ 对话
    async def append_turns(self, session_id: str, turns: list[Turn]) -> None:
        if not turns:
            return
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            await connection.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES ($1, $2, $3, $4)",
                [(session_id, turn.role, turn.content, turn.ts) for turn in turns],
            )

    async def history(self, session_id: str, limit: int | None = None) -> list[Turn]:
        pool = await self._pool_or_init()
        size = limit or self._max_turns
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id = $1 ORDER BY id DESC LIMIT $2",
                session_id,
                size,
            )
        return [
            Turn(role=row["role"], content=row["content"], ts=row["created_at"])
            for row in reversed(rows)
        ]

    async def turns_between(self, session_id: str, start_ts: float, end_ts: float) -> list[Turn]:
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id = $1 AND created_at >= to_timestamp($2) "
                "AND created_at < to_timestamp($3) ORDER BY id",
                session_id,
                start_ts,
                end_ts,
            )
        return [
            Turn(role=row["role"], content=row["content"], ts=row["created_at"]) for row in rows
        ]

    # ------------------------------------------------------------------ 记忆
    async def add_memories(self, session_id: str, items: list[MemoryItem]) -> int:
        if not items:
            return 0
        normalized = [item.normalized() for item in items]
        vectors = await self._embed(
            [f"{item.content} {item.keywords}".strip() for item in normalized]
        )
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            for index, item in enumerate(normalized):
                vector = None
                if vectors:
                    vector = _to_vector_literal(vectors[index])
                await connection.execute(
                    "INSERT INTO memories "
                    "(session_id, kind, content, keywords, importance, confidence, "
                    "source, embedding) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)",
                    session_id,
                    item.kind,
                    item.content,
                    item.keywords,
                    item.importance,
                    item.confidence,
                    item.source,
                    vector,
                )
        return len(normalized)

    async def recall(self, session_id: str, query: str, limit: int = 6) -> list[RecalledMemory]:
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, kind, content, keywords, importance, created_at, "
                "embedding::text AS embedding "
                "FROM memories WHERE session_id = $1 AND archived = FALSE "
                "ORDER BY created_at DESC LIMIT $2",
                session_id,
                self._max_candidates,
            )
        if not rows:
            return []

        candidates: list[RecalledMemory] = []
        embeddings: dict[int, list[float]] = {}
        for index, row in enumerate(rows):
            candidates.append(
                RecalledMemory(
                    content=row["content"],
                    kind=row["kind"],
                    importance=float(row["importance"]),
                    created_at=row["created_at"],
                    extra={"keywords": row["keywords"], "id": row["id"]},
                )
            )
            vector = _parse_vector(row["embedding"])
            if vector:
                embeddings[index] = vector

        query_vector = None
        if self._embedder is not None:
            embedded = await self._embed([query])
            if embedded:
                query_vector = embedded[0]

        ranked = rank_memories(
            query=query,
            candidates=candidates,
            now=datetime.now(UTC),
            half_life_days=self._half_life_days,
            query_embedding=query_vector,
            candidate_embeddings=embeddings or None,
            limit=limit,
        )
        ids = [item.extra["id"] for item in ranked if item.extra.get("id")]
        if ids:
            async with pool.acquire() as connection:
                await connection.execute(
                    "UPDATE memories SET last_accessed_at = now(), access_count = access_count + 1 "
                    "WHERE id = ANY($1::bigint[])",
                    ids,
                )
        return ranked

    async def upsert_relations(self, session_id: str, items: list[RelationItem]) -> None:
        pairs = [(item.key.strip()[:64], item.value.strip()[:200]) for item in items]
        pairs = [(key, value) for key, value in pairs if key and value]
        if not pairs:
            return
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            await connection.executemany(
                "INSERT INTO relations (session_id, key, value) VALUES ($1, $2, $3) "
                "ON CONFLICT (session_id, key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now()",
                [(session_id, key, value) for key, value in pairs],
            )

    async def add_episode(self, session_id: str, summary: str) -> None:
        summary = summary.strip()[:500]
        if not summary:
            return
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO episodes (session_id, day, summary) VALUES ($1, CURRENT_DATE, $2) "
                "ON CONFLICT (session_id, day) DO UPDATE SET summary = EXCLUDED.summary",
                session_id,
                summary,
            )

    async def profile(self, session_id: str) -> dict[str, str]:
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT key, value FROM relations WHERE session_id = $1 ORDER BY updated_at DESC",
                session_id,
            )
        return {row["key"]: row["value"] for row in rows}

    async def episodes(self, session_id: str, limit: int = 3) -> list[str]:
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT summary FROM episodes WHERE session_id = $1 ORDER BY day DESC LIMIT $2",
                session_id,
                limit,
            )
        return [row["summary"] for row in reversed(rows)]

    async def clear(self, session_id: str) -> None:
        pool = await self._pool_or_init()
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM messages WHERE session_id = $1", session_id)
            await connection.execute("DELETE FROM memories WHERE session_id = $1", session_id)
            await connection.execute("DELETE FROM relations WHERE session_id = $1", session_id)
            await connection.execute("DELETE FROM episodes WHERE session_id = $1", session_id)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
