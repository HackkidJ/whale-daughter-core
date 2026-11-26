"""记忆条目与检索结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class MemoryItem:
    """一条长期记忆（事实/偏好/事件/关系）。"""

    content: str
    kind: str = "fact"  # fact | preference | event | relation
    keywords: str = ""
    importance: float = 0.5
    confidence: float = 0.8
    source: str = ""

    def normalized(self) -> MemoryItem:
        return MemoryItem(
            content=self.content.strip()[:500],
            kind=self.kind if self.kind in {"fact", "preference", "event", "relation"} else "fact",
            keywords=self.keywords.strip()[:200],
            importance=min(1.0, max(0.0, float(self.importance))),
            confidence=min(1.0, max(0.0, float(self.confidence))),
            source=self.source,
        )


@dataclass(slots=True)
class RelationItem:
    key: str
    value: str


@dataclass(slots=True)
class RecalledMemory:
    content: str
    kind: str
    importance: float
    created_at: datetime | None = None
    score: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    memories: list[MemoryItem] = field(default_factory=list)
    relations: list[RelationItem] = field(default_factory=list)
    episode: str = ""
