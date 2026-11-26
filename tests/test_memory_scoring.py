"""检索打分：词面相似度、时间衰减、混合排序。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dsdaughter.memory.base import RecalledMemory
from dsdaughter.memory.scoring import (
    bigrams,
    cosine,
    lexical_similarity,
    rank_memories,
    recency_score,
)


def test_bigrams_ignore_punctuation():
    assert bigrams("老爸，喜欢 美式！") == bigrams("老爸喜欢美式")


def test_lexical_favors_query_coverage():
    query = "老爸喜欢喝什么咖啡"
    close = lexical_similarity(query, "老爸喜欢喝不加糖的美式咖啡")
    far = lexical_similarity(query, "今天天气不错")
    assert close > far
    assert 0.0 <= far <= 0.4


def test_recency_decay_half_life():
    now = datetime.now(UTC)
    fresh = recency_score(now, now, 30)
    month = recency_score(now - timedelta(days=30), now, 30)
    old = recency_score(now - timedelta(days=180), now, 30)
    assert fresh == 1.0
    assert 0.45 < month < 0.55
    assert old < 0.05


def test_cosine_bounds():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0


def _candidate(content: str, *, importance: float = 0.5, days_ago: float = 0.0, keywords: str = ""):
    return RecalledMemory(
        content=content,
        kind="fact",
        importance=importance,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
        extra={"keywords": keywords},
    )


def test_rank_prefers_relevant_and_recent():
    now = datetime.now(UTC)
    candidates = [
        _candidate("今天天气不错", importance=0.9, days_ago=0),
        _candidate("老爸喜欢喝不加糖的美式", importance=0.6, days_ago=3),
    ]
    ranked = rank_memories(query="老爸的咖啡口味", candidates=candidates, now=now, limit=2)
    assert ranked[0].content.startswith("老爸喜欢")


def test_rank_vector_can_lift_candidate():
    now = datetime.now(UTC)
    candidates = [_candidate("完全无关的一句话", importance=0.2, days_ago=60)]
    ranked = rank_memories(
        query="完全不同的问题",
        candidates=candidates,
        now=now,
        query_embedding=[1.0, 0.0],
        candidate_embeddings={0: [1.0, 0.0]},
        limit=3,
    )
    assert ranked and ranked[0].score > 0.5


def test_rank_respects_limit_and_threshold():
    now = datetime.now(UTC)
    candidates = [_candidate(f"记忆 {index}") for index in range(20)]
    ranked = rank_memories(query="记忆", candidates=candidates, now=now, limit=5)
    assert len(ranked) == 5
    assert all(item.score >= 0.12 for item in ranked)
