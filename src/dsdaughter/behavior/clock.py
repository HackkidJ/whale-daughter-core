"""生物钟：作息、困倦、失眠、晚安窗口、想念阈值。"""

from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo


def parse_hm(value: str, default: tuple[int, int] = (0, 0)) -> time:
    try:
        hour, _, minute = str(value).partition(":")
        return time(int(hour), int(minute or 0))
    except (ValueError, TypeError):
        return time(*default)


def _minutes(day: datetime) -> int:
    return day.hour * 60 + day.minute


def in_window(now: datetime, start: str, end: str) -> bool:
    """支持跨零点的窗口，例如 23:00-02:00。"""
    begin = parse_hm(start)
    finish = parse_hm(end)
    current = now.time()
    if begin <= finish:
        return begin <= current < finish
    return current >= begin or current < finish


class BioClock:
    def __init__(self, settings, tz: ZoneInfo | None = None) -> None:
        self.settings = settings
        self.tz = tz or ZoneInfo(settings.timezone)

    # ------------------------------------------------------------------ 作息
    def sleep_window(self, now: datetime) -> tuple[str, str]:
        if now.weekday() >= 5:  # 周末
            return self.settings.weekend_sleep_start, self.settings.weekend_sleep_end
        return self.settings.sleep_start, self.settings.sleep_end

    def is_asleep(self, now: datetime) -> bool:
        start, end = self.sleep_window(now)
        return in_window(now, start, end)

    def is_active_hours(self, now: datetime) -> bool:
        """适合她主动找人的时段：起床后到睡前（不含睡觉）。"""
        return not self.is_asleep(now) and 8 <= now.hour < 23

    # ------------------------------------------------------------------ 情绪/精力
    def energy(self, now: datetime) -> float:
        minutes = _minutes(now)
        # 0~1 的正弦：早上 9 点最高、凌晨 3 点最低
        phase = (minutes - 180) / 1440 * 2 * math.pi
        value = 0.55 + 0.35 * math.cos(phase)
        if self.is_asleep(now):
            value = min(value, 0.25)
        return round(max(0.05, min(1.0, value)), 2)

    def mood_from_energy(self, energy: float, now: datetime) -> str:
        if self.is_asleep(now):
            return "困倦"
        if energy >= 0.8:
            return "开心"
        if energy <= 0.3:
            return "低落"
        return "平静"

    # ------------------------------------------------------------------ 窗口判断
    def in_goodnight_window(self, now: datetime) -> bool:
        return in_window(
            now, self.settings.goodnight_window_start, self.settings.goodnight_window_end
        )

    def insomnia(self, now: datetime) -> bool:
        """凌晨 2 点后还醒着 = 失眠状态（用来关心对方，也给自己的语气用）。"""
        return in_window(now, self.settings.insomnia_after, self.settings.insomnia_until)

    def goodnight_time(self, now: datetime) -> str:
        """晚安触发时间：窗口起点后随机一点，固定在 23:00 附近。"""
        return self.settings.goodnight_window_start

    def sleep_context(self, now: datetime) -> str:
        if self.insomnia(now):
            return (
                f"现在是 {now.strftime('%H:%M')}，都两点多了，你还没睡——你在失眠吧。"
                "你现在又困又担心他，说话可以迷迷糊糊的，但别编造做过的事。"
            )
        if self.is_asleep(now):
            return (
                f"现在是 {now.strftime('%H:%M')}，你正在睡觉，被消息吵醒了。"
                "你还没完全清醒，很困，可以有点起床气、说话短一点。"
            )
        return ""

    def miss_due(self, last_user_at: float, now: datetime) -> bool:
        if not last_user_at or not self.is_active_hours(now):
            return False
        silence_hours = (now.timestamp() - last_user_at) / 3600
        return silence_hours >= float(self.settings.miss_hours)
