"""她"什么时候发语音"的决策：合理随机、像人，而不是每条都语音。

真实的人：大部分时候打字，情绪上来/想撒娇/对方先发语音时才回语音；
连续语音很少；太长的内容会打字；深夜会压低概率。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

_EMOTION_HINTS = ("想你", "晚安", "喜欢", "难过", "哼", "呜呜", "抱", "亲", "想你啦", "别走")
_URL_HINTS = ("http://", "https://", "www.", "```", "```python")


@dataclass(slots=True)
class VoiceDecision:
    use: bool
    probability: float
    reason: str


def decide_voice(
    text: str,
    *,
    settings,
    rng: random.Random | None = None,
    incoming_voice: bool = False,
    recent_count: int = 0,
    last_was_voice: bool = False,
    hour: int | None = None,
) -> VoiceDecision:
    """返回是否发语音 + 理由（理由会进日志，方便调参）。"""
    rng = rng or random.Random()
    if not getattr(settings, "voice_enabled", False):
        return VoiceDecision(False, 0.0, "语音关闭")

    content = (text or "").strip()
    if not content:
        return VoiceDecision(False, 0.0, "空内容")
    max_chars = int(getattr(settings, "voice_max_chars", 120))
    if len(content) > max_chars:
        return VoiceDecision(False, 0.0, f"太长（{len(content)}>{max_chars} 字，打字更合适）")
    if any(hint in content for hint in _URL_HINTS):
        return VoiceDecision(False, 0.0, "含链接/代码，语音没法念")

    max_per_hour = int(getattr(settings, "voice_max_per_hour", 4))
    if recent_count >= max_per_hour:
        return VoiceDecision(False, 0.0, f"这一小时已经发了 {recent_count} 条语音")

    probability = float(getattr(settings, "voice_probability", 0.25))
    reason = "基础概率"

    if incoming_voice:
        probability = float(getattr(settings, "voice_reply_to_voice", 0.85))
        reason = "他先发了语音，回语音"
    elif last_was_voice:
        probability *= 0.3
        reason = "上一条也是语音，降频"

    if any(hint in content for hint in _EMOTION_HINTS):
        probability = min(1.0, probability + 0.15)
        reason += " + 情绪词"

    if hour is not None:
        if hour >= 23 or hour < 7:
            probability *= 0.6
            reason += " + 深夜降频"
        elif 19 <= hour <= 22:
            probability = min(1.0, probability + 0.05)
            reason += " + 晚上更容易撒娇"

    draw = rng.random()
    return VoiceDecision(draw < probability, round(probability, 3), f"{reason}（掷出 {draw:.2f}）")
