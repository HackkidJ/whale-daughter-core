"""行为上下文：把生物钟/情绪/近期日记注入 Prompt。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class BehaviorContext:
    def __init__(self, store, clock, settings) -> None:
        self.store = store
        self.clock = clock
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)

    async def prompt_block(self) -> str:
        now = datetime.now(self.tz)
        emotion = await self.store.get_emotion()
        lines = ["【当前生理/情绪状态｜系统自动注入】", f"- 现在：{now.strftime('%Y-%m-%d %H:%M')}"]
        sleep_ctx = self.clock.sleep_context(now)
        if sleep_ctx:
            lines.append(f"- {sleep_ctx}")
        lines.append(f"- 情绪：{emotion.mood}（精力 {emotion.energy:.2f}）")
        diaries = await self.store.recent_diaries(self.settings.diary_inject_limit)
        if diaries:
            lines.append("【近期日记｜只是内心回忆，不是刚刚发生的事实】")
            for diary in diaries:
                text = " ".join(diary.content.split())[: self.settings.diary_inject_chars]
                lines.append(f"- {diary.day}：{text}")
            lines.append("- 日记和情绪是内心状态，不是已执行的动作；不要把日记内容说成刚发生的事。")
        return "\n".join(lines)
