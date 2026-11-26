"""消息通道层：QQ（OneBot 11）、未来的 B 站 / Telegram 等。

每个通道负责两件事：把平台事件翻译成统一的输入，把回复翻译回平台格式。
通道不碰人格和记忆 —— 那是 agent 的事。
"""

from __future__ import annotations

from .onebot import (
    MessageEvent,
    OneBotError,
    OneBotServer,
    Segment,
    at_segment,
    parse_segments,
    split_text,
    text_segment,
)
from .qq import QQChannel, create_router

__all__ = [
    "MessageEvent",
    "OneBotError",
    "OneBotServer",
    "QQChannel",
    "Segment",
    "at_segment",
    "create_router",
    "parse_segments",
    "split_text",
    "text_segment",
]
