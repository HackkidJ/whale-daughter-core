"""WAV → silk（QQ 语音消息格式）。

QQ 的语音消息必须是 silk 编码；NapCat 的 `record` 段支持 `base64://`。
用 pilk（GPL-3.0）做编码：它是文件式 API，所以走临时文件 + 线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import wave

import structlog

log = structlog.get_logger(__name__)


class SilkError(RuntimeError):
    """silk 编码失败。"""


# QQ/微信 的 silk 文件要求以 0x02 开头（腾讯私有头）；少了它手机 QQ 会"点一下就停"。
TENCENT_SILK_PREFIX = b"\x02"


def ensure_tencent_silk(silk: bytes) -> bytes:
    """给标准 silk 补上腾讯 0x02 头；已经是腾讯格式的原样返回。"""
    if not silk:
        raise SilkError("空 silk")
    if silk.startswith(TENCENT_SILK_PREFIX):
        return silk
    return TENCENT_SILK_PREFIX + silk


def wav_to_silk_sync(wav_bytes: bytes) -> bytes:
    """同步编码（放线程池里调）。"""
    try:
        import pilk
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给清晰错误
        raise SilkError("没有安装 pilk，无法编码 silk") from exc

    with tempfile.TemporaryDirectory(prefix="dsd-silk-") as tmp:
        wav_path = os.path.join(tmp, "in.wav")
        pcm_path = os.path.join(tmp, "in.pcm")
        silk_path = os.path.join(tmp, "out.silk")
        with open(wav_path, "wb") as handle:
            handle.write(wav_bytes)
        try:
            with wave.open(wav_path, "rb") as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise SilkError("silk 只支持单声道 16bit WAV")
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
        except wave.Error as exc:
            raise SilkError(f"WAV 解析失败：{exc}") from exc
        with open(pcm_path, "wb") as handle:
            handle.write(frames)
        try:
            pilk.encode(pcm_path, silk_path, pcm_rate=rate)
        except Exception as exc:
            raise SilkError(f"silk 编码失败：{exc}") from exc
        with open(silk_path, "rb") as handle:
            return ensure_tencent_silk(handle.read())


async def wav_to_silk(wav_bytes: bytes) -> bytes:
    return await asyncio.to_thread(wav_to_silk_sync, wav_bytes)
