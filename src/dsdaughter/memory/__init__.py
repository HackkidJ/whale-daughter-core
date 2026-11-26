"""长期记忆层。

P2a：Postgres + pgvector。
- messages：原始对话流水（短期上下文）
- memories：抽取出来的长期事实（可带 embedding）
- relations：稳定画像（称呼/生日/喜好/雷点）
- episodes：按天的一句话总结

检索 = 词面相似度 + 时间衰减 + 重要度（+ 可选向量相似度）。
"""

from __future__ import annotations

from .base import ExtractionResult, MemoryItem, RecalledMemory, RelationItem
from .embedding import Embedder, OpenAICompatEmbedder, build_embedder
from .extract import MemoryExtractor, parse_extraction
from .inmemory import InMemoryMemoryStore, MemoryStore
from .postgres import PostgresMemoryStore
from .scoring import bigrams, cosine, lexical_similarity, rank_memories, recency_score

__all__ = [
    "Embedder",
    "ExtractionResult",
    "InMemoryMemoryStore",
    "MemoryExtractor",
    "MemoryItem",
    "MemoryStore",
    "OpenAICompatEmbedder",
    "PostgresMemoryStore",
    "RecalledMemory",
    "RelationItem",
    "bigrams",
    "build_embedder",
    "build_memory_store",
    "cosine",
    "lexical_similarity",
    "parse_extraction",
    "rank_memories",
    "recency_score",
]


def build_memory_store(settings) -> MemoryStore:
    """按配置选后端：memory（进程内）或 postgres（持久化）。"""
    if settings.memory_backend == "postgres":
        return PostgresMemoryStore(
            settings.database_url,
            max_turns=settings.history_turns,
            embedder=build_embedder(settings),
            dimension=settings.embedding_dim,
            half_life_days=settings.memory_half_life_days,
            max_candidates=settings.memory_max_candidates,
        )
    return InMemoryMemoryStore(max_turns=settings.history_turns)
