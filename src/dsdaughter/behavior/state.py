"""行为状态：情绪 / 活动 / 日记。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class EmotionState:
    mood: str = "平静"  # 开心/平静/低落/委屈/困倦/失眠/兴奋
    energy: float = 0.8  # 0~1
    reason: str = ""
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ActivityRecord:
    last_user_at: float = 0.0
    last_reply_at: float = 0.0
    last_proactive_at: float = 0.0
    last_goodnight_day: str = ""


@dataclass(slots=True)
class Diary:
    day: str
    content: str
    created_at: float = 0.0
