"""检索打分：词面相似度 + 时间衰减 + 重要度，可叠加向量相似度。

为什么不全交给 pgvector：中文短记忆（"老爸喜欢喝美式"）最长也就一两句，
字符二元组覆盖率比稠密向量更稳，而且零依赖、可解释。有 embedding 时叠加即可。
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from .base import RecalledMemory

_PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）()\[\]【】,.!?;:~\-…]+")


def bigrams(text: str) -> set[str]:
    cleaned = _PUNCT.sub("", text.lower())
    if not cleaned:
        return set()
    if len(cleaned) == 1:
        return {cleaned}
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def lexical_similarity(query: str, content: str, keywords: str = "") -> float:
    """查询词被记忆覆盖的比例（0~1），兼顾记忆本身的精确度。"""
    query_grams = bigrams(query)
    if not query_grams:
        return 0.0
    memory_grams = bigrams(f"{content} {keywords}")
    if not memory_grams:
        return 0.0
    overlap = len(query_grams & memory_grams)
    recall = overlap / len(query_grams)
    precision = overlap / len(memory_grams)
    return 0.75 * recall + 0.25 * precision


def recency_score(created_at: datetime | None, now: datetime, half_life_days: float) -> float:
    if created_at is None:
        return 0.5
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    half_life = max(0.5, half_life_days)
    return math.pow(0.5, age_days / half_life)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


def rank_memories(
    *,
    query: str,
    candidates: list[RecalledMemory],
    now: datetime,
    half_life_days: float = 30.0,
    query_embedding: list[float] | None = None,
    candidate_embeddings: dict[int, list[float]] | None = None,
    weights: tuple[float, float, float] = (0.55, 0.25, 0.20),
    limit: int = 6,
    min_score: float = 0.12,
) -> list[RecalledMemory]:
    """score = w_sim * max(lexical, cosine?) + w_rec * 衰减 + w_imp * 重要度"""
    weight_sim, weight_rec, weight_imp = weights
    scored: list[RecalledMemory] = []
    for index, candidate in enumerate(candidates):
        lexical = lexical_similarity(
            query, candidate.content, str(candidate.extra.get("keywords", ""))
        )
        similarity = lexical
        if query_embedding and candidate_embeddings:
            embedding = candidate_embeddings.get(index)
            if embedding:
                vector_similarity = cosine(query_embedding, embedding)
                similarity = max(similarity, vector_similarity * 0.95)
        recency = recency_score(candidate.created_at, now, half_life_days)
        score = weight_sim * similarity + weight_rec * recency + weight_imp * candidate.importance
        if similarity <= 0 and not query_embedding:
            score *= 0.65  # 纯靠新近和重要度召回，降权
        candidate.score = round(score, 4)
        if candidate.score >= min_score:
            scored.append(candidate)
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:limit]
