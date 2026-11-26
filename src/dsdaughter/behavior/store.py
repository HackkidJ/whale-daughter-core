"""行为状态持久化：SQLite（独立于 Postgres 记忆库，坏了自己吞掉，不影响聊天）。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from .state import Diary, EmotionState

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emotion (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mood TEXT NOT NULL,
  energy REAL NOT NULL,
  reason TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS diary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT UNIQUE NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


class BehaviorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ 基础
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def _run(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:  # 行为库坏了绝不影响主对话
            log.warning("behavior_store_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------ state
    def _get_state_sync(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM state").fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def get_state(self) -> dict[str, Any]:
        return await self._run(self._get_state_sync) or {}

    def _set_state_sync(self, values: dict[str, Any]) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO state(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (key, str(value), now),
                )

    async def set_state(self, **values: Any) -> None:
        await self._run(self._set_state_sync, values)

    def _get_value_sync(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    async def get_value(self, key: str, default: str = "") -> str:
        return (await self._run(self._get_value_sync, key, default)) or default

    # ------------------------------------------------------------------ emotion
    def _latest_emotion_sync(self) -> EmotionState:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT mood, energy, reason, created_at FROM emotion ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return EmotionState()
        return EmotionState(
            mood=row["mood"],
            energy=float(row["energy"]),
            reason=row["reason"] or "",
            updated_at=float(row["created_at"]),
        )

    async def get_emotion(self) -> EmotionState:
        return await self._run(self._latest_emotion_sync) or EmotionState()

    def _set_emotion_sync(self, mood: str, energy: float, reason: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO emotion(mood, energy, reason, created_at) VALUES(?, ?, ?, ?)",
                (mood, float(energy), reason, time.time()),
            )

    async def set_emotion(self, mood: str, energy: float, reason: str = "") -> None:
        await self._run(self._set_emotion_sync, mood, energy, reason)

    # ------------------------------------------------------------------ diary
    def _add_diary_sync(self, day: str, content: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO diary(day, content, created_at) VALUES(?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET content=excluded.content",
                (day, content, time.time()),
            )

    async def add_diary(self, day: str, content: str) -> None:
        await self._run(self._add_diary_sync, day, content)

    def _recent_diaries_sync(self, limit: int) -> list[Diary]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT day, content, created_at FROM diary ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Diary(day=row["day"], content=row["content"], created_at=float(row["created_at"]))
            for row in rows
        ]

    async def recent_diaries(self, limit: int = 3) -> list[Diary]:
        return await self._run(self._recent_diaries_sync, limit) or []

    # ------------------------------------------------------------------ proactive log
    def _log_proactive_sync(self, kind: str, text: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO proactive_log(kind, text, created_at) VALUES(?, ?, ?)",
                (kind, text, time.time()),
            )

    async def log_proactive(self, kind: str, text: str) -> None:
        await self._run(self._log_proactive_sync, kind, text)

    def _count_today_sync(self, kind: str | None = None) -> int:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self._lock, self._connect() as conn:
            if kind:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM proactive_log WHERE created_at>=? AND kind=?",
                    (start, kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM proactive_log WHERE created_at>=?", (start,)
                ).fetchone()
        return int(row["n"]) if row else 0

    async def count_proactive_today(self, kind: str | None = None) -> int:
        return int(await self._run(self._count_today_sync, kind) or 0)

    def _last_proactive_sync(self, kind: str) -> float:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM proactive_log WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return float(row["created_at"]) if row else 0.0

    async def last_proactive_at(self, kind: str) -> float:
        return float(await self._run(self._last_proactive_sync, kind) or 0.0)

    async def aclose(self) -> None:
        return None
