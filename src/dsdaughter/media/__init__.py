"""媒体能力：TTS 语音、QQ 语音编码、歌库。"""

from __future__ import annotations

from .silk import (
    TENCENT_SILK_PREFIX,
    SilkError,
    ensure_tencent_silk,
    wav_to_silk,
    wav_to_silk_sync,
)
from .songs import Song, SongLibrary
from .tts import (
    DeepSeekTTS,
    FakeTTS,
    TTSProvider,
    VoiceError,
    VolcengineTTS,
    build_tts,
    wav_info,
)
from .voice_policy import VoiceDecision, decide_voice

__all__ = [
    "DeepSeekTTS",
    "FakeTTS",
    "SilkError",
    "TENCENT_SILK_PREFIX",
    "ensure_tencent_silk",
    "Song",
    "SongLibrary",
    "TTSProvider",
    "VolcengineTTS",
    "VoiceDecision",
    "VoiceError",
    "build_tts",
    "decide_voice",
    "wav_info",
    "wav_to_silk",
    "wav_to_silk_sync",
]
