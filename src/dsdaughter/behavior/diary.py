"""凌晨日记：读当天聊天 → 第一人称日记 → 存 SQLite。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import structlog

from ..types import Message

log = structlog.get_logger(__name__)

_DIARY_PROMPT = """你是{name}。请根据下面这一天的聊天记录，用第一人称写一篇日记（100~300 字）。

要求：
- 像真的日记：只写你今天的感受、在意的小事、对对方的观察；不要逐条复述聊天。
- 可以有小情绪、小吐槽；不要编造聊天里没发生的物理事件。
- 不要写日期标题、不要用 Markdown、不要写"今天"以外的总结腔。

聊天记录：
{transcript}
"""


class DiaryWriter:
    def __init__(self, agent, store, settings) -> None:
        self.agent = agent
        self.store = store
        self.settings = settings

    def _day_range(self, day: date) -> tuple[float, float]:
        start = datetime.combine(day, time.min, tzinfo=self._tz).timestamp()
        return start, start + 86400

    @property
    def _tz(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.settings.timezone)

    async def write_for(self, day: date, session_id: str) -> str | None:
        start, end = self._day_range(day)
        turns = await self.agent.memory.turns_between(session_id, start, end)
        if len(turns) < int(self.settings.diary_min_turns):
            return None
        transcript = "\n".join(
            f"{'对方' if turn.role == 'user' else '我'}：{turn.content[:200]}"
            for turn in turns[-80:]
        )
        prompt = _DIARY_PROMPT.format(name=self.agent.persona.name, transcript=transcript)
        messages = [
            Message("system", "你写日记时不用讨好任何人，诚实、简短、像自己。"),
            Message("user", prompt),
        ]
        result_text = ""
        try:
            async for event in self.agent.llm.stream(
                messages, web=False, thinking=False, tools=None
            ):
                if event.kind == "done":
                    result_text = event.payload["result"].text
        except Exception as exc:
            log.warning("diary_llm_failed", error=str(exc))
            return None
        content = " ".join(result_text.split()).strip()
        if not content:
            return None
        await self.store.add_diary(day.isoformat(), content)
        await self.store.set_state(last_diary_day=day.isoformat())
        log.info("diary_written", day=day.isoformat(), chars=len(content))
        return content

    def yesterday(self) -> date:
        return date.today() - timedelta(days=1)
