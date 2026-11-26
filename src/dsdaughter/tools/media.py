"""媒体工具：让她发语音、唱歌。

她可以：
- `speak`：把一句话用语音发出来（QQ 语音消息）
- `sing`：从歌库挑一首"她唱的歌"发出去（离线预生成的 SVC/歌声合成素材）
"""

from __future__ import annotations

import random

import structlog

from ..media.songs import SongLibrary
from ..media.tts import wav_info
from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)


def _channel(ctx: ToolContext):
    return ctx.require_qq()


def _resolve_target(ctx: ToolContext) -> tuple[int, int | None]:
    """从 session_id 解析出"发给谁"：私聊用户 / 群 / 临时会话。"""
    channel = _channel(ctx)
    session = ctx.session_id
    parts = session.split(":")
    if session.startswith("qq:g:") and len(parts) >= 5:
        return int(parts[4]), int(parts[2])
    if session.startswith("qq:u:") and len(parts) >= 3:
        return int(parts[2]), None
    if session.startswith("qq:temp:") and len(parts) >= 3:
        user_id = int(parts[2])
        return user_id, channel.contacts.temp_group(user_id)
    owner = channel.contacts.owner
    if owner:
        return owner, None
    raise ToolError("现在不在 QQ 会话里，发不了语音")


async def speak(ctx: ToolContext, text: str) -> ToolResult:
    channel = _channel(ctx)
    content = str(text or "").strip()
    if not content:
        raise ToolError("要说点什么呀")
    if len(content) > 300:
        raise ToolError("语音太长了，300 字以内")
    tts = getattr(channel, "tts", None)
    if tts is None:
        raise ToolError("语音功能没开（检查 DSD_VOICE_ENABLED / API Key）")
    wav, _rate = await tts.synthesize(content)
    user_id, group_id = _resolve_target(ctx)
    await channel.send_voice_to(user_id, wav, group_id=group_id)
    duration = round(wav_info(wav)[1], 2)
    return ToolResult.success(f"语音发过去了（{duration} 秒）。", duration=duration)


async def sing(ctx: ToolContext, song: str = "") -> ToolResult:
    channel = _channel(ctx)
    if not getattr(channel.settings, "sing_enabled", True):
        raise ToolError("唱歌功能没开")
    library = SongLibrary(channel.settings.songs_dir)
    if not library.songs:
        raise ToolError(
            "歌库是空的。让老爸往 assets/songs/ 里放几首用我声音做的歌"
            "（manifest.json 里登记标题和文件名），我就能唱了。"
        )
    picked = library.pick(str(song or ""), rng=random.Random())
    if picked is None:
        raise ToolError("没找到这首歌，换一首？")
    user_id, group_id = _resolve_target(ctx)
    payload = picked.file.read_bytes()
    if picked.file.suffix.lower() == ".silk":
        await channel.send_silk_to(user_id, payload, group_id=group_id)
    else:
        await channel.send_voice_to(user_id, payload, group_id=group_id)
    log.info("她唱了一首", song=picked.title, user_id=user_id)
    duration = (
        picked.duration or round(wav_info(payload)[1], 1)
        if picked.file.suffix != ".silk"
        else picked.duration
    )
    return ToolResult.success(
        f"我唱了《{picked.title}》发给他了。", song=picked.title, duration=duration
    )


def build_media_tools() -> list[Tool]:
    return [
        Tool(
            name="speak",
            description=(
                "用语音把一句话说出来发给他（QQ 语音消息）。"
                "想撒娇、说晚安、或者对方让你发语音时用；内容太长就打字。"
            ),
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要说的内容，300 字以内"}},
                "required": ["text"],
            },
            handler=speak,
            is_action=True,
        ),
        Tool(
            name="sing",
            description=(
                "挑一首歌唱歌给他听（歌库里你唱好的音频）。"
                "他说「唱首歌」「唱个XXX」时用；歌库空的时候如实说还没有歌。"
            ),
            parameters={
                "type": "object",
                "properties": {"song": {"type": "string", "description": "歌名关键词，可留空随机"}},
            },
            handler=sing,
            is_action=True,
        ),
    ]
