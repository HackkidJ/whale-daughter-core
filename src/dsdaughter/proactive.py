"""主动性：她会自己找你说话，但不会无脑刷屏。

规则（"黏人"但不烦人）：
- 按他的作息挑时机：上班前、午休、下班、睡前、周末上午各一次；其余时间随机"想你了"
- 全局最小间隔 + 每日上限 + 免打扰时段（她也要睡觉）
- 刚聊过就不主动插话（避免"你还没回我又发一条"）
- 每次最多一两句，由她自己组织语言（复用完整人设 + 对方画像 + 记忆）

状态落库（Postgres），所以重启后不会立刻重复问候。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg
import structlog

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------- 触发
@dataclass(slots=True)
class Trigger:
    kind: str
    hint: str


@dataclass(slots=True)
class ProactiveState:
    kinds_today: set[str] = field(default_factory=set)
    count_today: int = 0
    last_sent_at: datetime | None = None


def _to_minutes(value: str) -> int:
    hour, _, minute = value.strip().partition(":")
    return int(hour) * 60 + int(minute or 0)


def in_quiet_hours(now: datetime, spec: str) -> bool:
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return False
    start_text, end_text = spec.split("-", 1)
    try:
        start, end = _to_minutes(start_text), _to_minutes(end_text)
    except ValueError:
        return False
    current = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end  # 跨午夜


# 固定日程窗口：kind, 起始分钟, 结束分钟, 是否只在工作日, 动机提示
_SCHEDULE: tuple[tuple[str, int, int, str, str], ...] = (
    (
        "morning_before_work",
        9 * 60 + 15,
        9 * 60 + 55,
        "workday",
        "他快上班了（10 点上班），小声问候一下",
    ),
    ("lunch", 11 * 60 + 50, 13 * 60 + 20, "workday", "午休时间，问问他吃了没"),
    ("off_work", 18 * 60 + 45, 19 * 60 + 40, "workday", "他刚下班，问问今天累不累"),
    (
        "weekend_morning",
        10 * 60 + 30,
        11 * 60 + 40,
        "weekend",
        "周末上午，逗他起床、问问睡到几点",
    ),
    ("goodnight", 22 * 60 + 20, 23 * 60 + 0, "any", "快睡觉了，说句晚安"),
)


def pick_trigger(
    *,
    now: datetime,
    state: ProactiveState,
    settings: Any,
    rng: random.Random | None = None,
) -> Trigger | None:
    """决定"现在要不要主动说话、说什么由头"。规则全部可预测，方便测试。"""
    if not settings.proactive_enabled:
        return None
    if state.count_today >= settings.proactive_daily_limit:
        return None
    if in_quiet_hours(now, settings.proactive_quiet_hours):
        return None
    if state.last_sent_at is not None:
        gap = (now - state.last_sent_at).total_seconds()
        if gap < settings.proactive_min_gap_minutes * 60:
            return None

    current = now.hour * 60 + now.minute
    is_workday = now.weekday() < 5
    for kind, start, end, scope, hint in _SCHEDULE:
        if scope == "workday" and not is_workday:
            continue
        if scope == "weekend" and is_workday:
            continue
        if not start <= current < end:
            continue
        if kind in state.kinds_today:
            continue
        return Trigger(kind=kind, hint=hint)

    # 随机"想你了"：只在距上次够久时掷骰子
    rng = rng or random.Random()
    if state.last_sent_at is None:
        gap_minutes = float("inf")
    else:
        gap_minutes = (now - state.last_sent_at).total_seconds() / 60
    if (
        gap_minutes >= settings.proactive_miss_gap_minutes
        and rng.random() < settings.proactive_miss_probability
    ):
        return Trigger(kind="miss", hint="没什么事，就是突然想找他说话，找个由头")
    return None


# --------------------------------------------------------------------- 状态存储
class ProactiveStore(Protocol):
    async def state(self, session_id: str, since: datetime) -> ProactiveState: ...

    async def record(self, session_id: str, user_id: str, kind: str, content: str) -> None: ...

    async def aclose(self) -> None: ...


class InMemoryProactiveStore:
    def __init__(self) -> None:
        self._rows: dict[str, list[tuple[str, datetime, str]]] = {}

    async def state(self, session_id: str, since: datetime) -> ProactiveState:
        rows = [row for row in self._rows.get(session_id, []) if row[1] >= since]
        last = max((row[1] for row in self._rows.get(session_id, [])), default=None)
        return ProactiveState(
            kinds_today={row[0] for row in rows},
            count_today=len(rows),
            last_sent_at=last,
        )

    async def record(self, session_id: str, user_id: str, kind: str, content: str) -> None:
        self._rows.setdefault(session_id, []).append((kind, datetime.now(UTC), content))

    async def aclose(self) -> None:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_sends (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_proactive_session
    ON proactive_sends (session_id, sent_at DESC);
"""


class PostgresProactiveStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2, timeout=10)
            assert pool is not None
            async with pool.acquire() as connection:
                await connection.execute(_SCHEMA)
            self._pool = pool
        return self._pool

    async def state(self, session_id: str, since: datetime) -> ProactiveState:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT kind, sent_at FROM proactive_sends "
                "WHERE session_id = $1 AND sent_at >= $2 ORDER BY sent_at DESC",
                session_id,
                since,
            )
        kinds = {row["kind"] for row in rows}
        last = max((row["sent_at"] for row in rows), default=None)
        if last is None:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT max(sent_at) AS last FROM proactive_sends WHERE session_id = $1",
                    session_id,
                )
            last = row["last"] if row else None
        return ProactiveState(kinds_today=kinds, count_today=len(rows), last_sent_at=last)

    async def record(self, session_id: str, user_id: str, kind: str, content: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO proactive_sends (session_id, user_id, kind, content) "
                "VALUES ($1, $2, $3, $4)",
                session_id,
                user_id,
                kind,
                content[:1000],
            )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def build_proactive_store(settings) -> ProactiveStore:
    if settings.memory_backend == "postgres":
        return PostgresProactiveStore(settings.database_url)
    return InMemoryProactiveStore()


# --------------------------------------------------------------------- 调度器
class ProactiveScheduler:
    def __init__(
        self,
        *,
        agent,
        channel,
        settings,
        store: ProactiveStore | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.agent = agent
        self.channel = channel
        self.settings = settings
        self.store = store or build_proactive_store(settings)
        self._rng = rng or random.Random()
        from zoneinfo import ZoneInfo

        self._tz = ZoneInfo(settings.timezone)

    def _targets(self) -> list[int]:
        raw = self.settings.proactive_target_users or self.settings.qq_allowed_users
        targets: list[int] = []
        for token in raw.replace("，", ",").split(","):
            token = token.strip()
            if token.isdigit():
                targets.append(int(token))
        return targets

    async def run(self) -> None:
        log.info(
            "主动性调度启动",
            tick_seconds=self.settings.proactive_tick_seconds,
            daily_limit=self.settings.proactive_daily_limit,
            quiet_hours=self.settings.proactive_quiet_hours,
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 调度器不能死
                log.warning("主动性 tick 失败", error=str(exc))
            await asyncio.sleep(max(5.0, self.settings.proactive_tick_seconds))

    async def tick(self, now: datetime | None = None) -> Trigger | None:
        if not self.settings.proactive_enabled:
            return None
        if self.channel is None or not self.channel.server.connected:
            return None
        targets = self._targets()
        if not targets:
            return None

        user_id = targets[0]
        session_id = f"qq:u:{user_id}"
        now = now or datetime.now(self._tz)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = day_start.astimezone(UTC)

        # 刚聊过就别插话
        try:
            history = await self.agent.memory.history(session_id, limit=1)
        except Exception:
            history = []
        if history:
            last_turn = history[-1]
            last_at = last_turn.ts if last_turn.ts.tzinfo else last_turn.ts.replace(tzinfo=UTC)
            if (now - last_at).total_seconds() < self.settings.proactive_min_gap_minutes * 60:
                return None

        state = await self.store.state(session_id, since)
        trigger = pick_trigger(now=now, state=state, settings=self.settings, rng=self._rng)
        if trigger is None:
            return None

        result = await self.agent.proactive_message(session_id, trigger.hint)
        text = result.text.strip()
        if not text:
            return None
        try:
            await self.channel.send_text(text=text, user_id=user_id)
        except Exception as exc:
            log.warning("主动消息发送失败", error=str(exc))
            return None

        await self.store.record(session_id, str(user_id), trigger.kind, text)
        log.info(
            "主动消息已发送",
            kind=trigger.kind,
            chars=len(text),
            user_id=user_id,
            daily_count=state.count_today + 1,
        )
        return trigger

    async def aclose(self) -> None:
        await self.store.aclose()
