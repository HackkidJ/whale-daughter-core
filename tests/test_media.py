"""语音：策略、TTS、silk、歌库、工具。"""

from __future__ import annotations

import base64
import io
import json
import struct
import wave
from pathlib import Path

import httpx
import pytest
import respx

from dsdaughter.config import Settings
from dsdaughter.media import (
    DeepSeekTTS,
    FakeTTS,
    SilkError,
    SongLibrary,
    VoiceError,
    decide_voice,
    wav_info,
    wav_to_silk_sync,
)
from dsdaughter.tools import ToolContext, ToolError
from dsdaughter.tools import media as media_tools


def make_wav(seconds: float = 0.3, rate: int = 24000) -> bytes:
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{frames}h", *([1000] * frames)))
    return buffer.getvalue()


def settings(**overrides) -> Settings:
    base = {
        "voice_enabled": True,
        "tts_provider": "fake",
        "voice_probability": 0.25,
        "voice_reply_to_voice": 0.85,
        "voice_max_chars": 120,
        "voice_max_per_hour": 4,
        "memory_extract": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------- 策略
def test_voice_policy_basics():
    s = settings()
    assert (
        decide_voice(
            "好呀。", settings=settings(voice_probability=1.0), rng=__import__("random").Random(0)
        ).use
        is True
    )
    assert decide_voice("好呀。", settings=settings(voice_probability=0.0)).use is False
    assert decide_voice("好呀。", settings=settings(voice_enabled=False)).use is False
    assert decide_voice("x" * 200, settings=s).use is False
    assert decide_voice("看这个 https://a.com", settings=s).use is False
    assert decide_voice("好呀。", settings=s, recent_count=4).use is False


def test_voice_policy_incoming_voice_boosts():
    import random

    s = settings(voice_probability=0.0)
    decision = decide_voice("好呀。", settings=s, incoming_voice=True, rng=random.Random(0))
    assert decision.use is True
    assert decision.probability >= 0.8


def test_voice_policy_consecutive_voice_lowers_probability():
    s = settings()
    import random

    first = decide_voice("好呀。", settings=s, rng=random.Random(0))
    second = decide_voice("在呢。", settings=s, last_was_voice=True, rng=random.Random(0))
    assert second.probability < first.probability


# --------------------------------------------------------------------- TTS / silk
@respx.mock
async def test_deepseek_tts_returns_wav():
    wav = make_wav(0.2)
    route = respx.post("https://api.deepseek.com/v1/tools/tts").mock(
        return_value=httpx.Response(200, content=wav)
    )
    tts = DeepSeekTTS(base_url="https://api.deepseek.com", api_key="sk-test")
    audio, rate = await tts.synthesize("你好呀")
    assert audio == wav
    assert rate == 24000
    payload = json.loads(route.calls[0].request.content)
    assert payload["voice_id"] == "stella"
    assert payload["format"] == "wav"


@respx.mock
async def test_deepseek_tts_error():
    respx.post("https://api.deepseek.com/v1/tools/tts").mock(
        return_value=httpx.Response(400, text="Content Exists Risk")
    )
    with pytest.raises(VoiceError):
        await DeepSeekTTS(base_url="https://api.deepseek.com", api_key="sk-test").synthesize("x")


def test_wav_info_fallback():
    rate, seconds = wav_info(make_wav(0.5))
    assert rate == 24000
    assert 0.4 < seconds < 0.6


def test_wav_info_handles_streaming_size_marker():
    payload = bytearray(make_wav(0.5))
    data_at = payload.find(b"data")
    assert data_at > 0
    payload[data_at + 4 : data_at + 8] = b"\xff\xff\xff\xff"
    rate, seconds = wav_info(bytes(payload))
    assert rate == 24000
    assert 0.3 < seconds < 0.8


def test_wav_to_silk_roundtrip():
    silk = wav_to_silk_sync(make_wav(0.3))
    assert len(silk) > 100
    # QQ 要求腾讯 0x02 头，否则手机端点一下就停
    assert silk.startswith(b"\x02#!SILK")


def test_ensure_tencent_silk_idempotent():
    from dsdaughter.media import ensure_tencent_silk

    standard = b"#!SILK_V3" + b"\x00" * 20
    once = ensure_tencent_silk(standard)
    assert once.startswith(b"\x02#!SILK")
    assert ensure_tencent_silk(once) == once


def test_wav_to_silk_rejects_stereo():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00" * 400)
    with pytest.raises(SilkError):
        wav_to_silk_sync(buffer.getvalue())


# --------------------------------------------------------------------- 歌库
def test_song_library_reads_manifest(tmp_path):
    (tmp_path / "a.wav").write_bytes(make_wav())
    (tmp_path / "manifest.json").write_text(
        json.dumps({"songs": [{"id": "a", "title": "小星星", "file": "a.wav", "duration": 3}]}),
        encoding="utf-8",
    )
    library = SongLibrary(tmp_path)
    assert library.titles() == ["小星星"]
    assert library.pick("小星星") is not None


def test_song_library_empty(tmp_path):
    library = SongLibrary(tmp_path)
    assert library.songs == []
    assert library.pick("") is None


# --------------------------------------------------------------------- 工具
class FakeChannel:
    def __init__(self, tmp_path: Path, settings_obj: Settings) -> None:
        self.settings = settings_obj
        self.tts = FakeTTS(0.2)
        self.contacts = type("C", (), {"owner": 1001, "temp_group": lambda self, uid: None})()
        self.voice_sent: list[tuple[int, bytes, int | None]] = []
        self.silk_sent: list[tuple[int, bytes, int | None]] = []
        self.server = type("S", (), {"connected": True, "self_id": 999})()

    async def send_voice_to(self, user_id, wav, *, group_id=None):
        self.voice_sent.append((user_id, wav, group_id))

    async def send_silk_to(self, user_id, silk, *, group_id=None):
        self.silk_sent.append((user_id, silk, group_id))


async def test_speak_tool(tmp_path):
    s = settings()
    channel = FakeChannel(tmp_path, s)
    ctx = ToolContext(settings=s, session_id="qq:u:1001", qq=channel)
    result = await media_tools.speak(ctx, text="晚安呀")
    assert result.ok
    assert channel.voice_sent and channel.voice_sent[0][0] == 1001


async def test_speak_without_voice_enabled(tmp_path):
    s = settings(voice_enabled=False)
    channel = FakeChannel(tmp_path, s)
    channel.tts = None
    ctx = ToolContext(settings=s, session_id="qq:u:1001", qq=channel)
    with pytest.raises(ToolError):
        await media_tools.speak(ctx, text="晚安")


async def test_sing_tool_without_songs(tmp_path):
    s = settings(songs_dir=tmp_path / "empty")
    channel = FakeChannel(tmp_path, s)
    ctx = ToolContext(settings=s, session_id="qq:u:1001", qq=channel)
    with pytest.raises(ToolError):
        await media_tools.sing(ctx)


async def test_sing_tool_sends_song(tmp_path):
    (tmp_path / "a.silk").write_bytes(b"#!SILK_V3" + b"\x00" * 100)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"songs": [{"id": "a", "title": "小星星", "file": "a.silk"}]}),
        encoding="utf-8",
    )
    s = settings(songs_dir=tmp_path)
    channel = FakeChannel(tmp_path, s)
    ctx = ToolContext(settings=s, session_id="qq:g:2001:u:1001", qq=channel)
    result = await media_tools.sing(ctx, song="小星星")
    assert result.ok
    assert result.data["song"] == "小星星"
    assert channel.silk_sent[0][0] == 1001


# --------------------------------------------------------------------- 火山引擎
@respx.mock
async def test_volcengine_tts_success():
    from dsdaughter.media import VolcengineTTS

    wav = make_wav(0.3)
    encoded = base64.b64encode(wav).decode()
    route = respx.post("https://openspeech.bytedance.com/api/v1/tts").mock(
        return_value=httpx.Response(200, json={"code": 3000, "message": "Success", "data": encoded})
    )
    tts = VolcengineTTS(
        app_id="app123",
        token="tok-abc",
        cluster="volcano_tts",
        voice_type="BV700_streaming",
    )
    audio, rate = await tts.synthesize("你好呀")
    assert audio == wav
    assert rate == 24000
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer;tok-abc"
    body = json.loads(request.content)
    assert body["app"]["appid"] == "app123"
    assert body["app"]["cluster"] == "volcano_tts"
    assert body["audio"]["voice_type"] == "BV700_streaming"
    assert body["request"]["text"] == "你好呀"


@respx.mock
async def test_volcengine_tts_error_code():
    from dsdaughter.media import VolcengineTTS

    respx.post("https://openspeech.bytedance.com/api/v1/tts").mock(
        return_value=httpx.Response(200, json={"code": 3001, "message": "invalid appid"})
    )
    with pytest.raises(VoiceError):
        await VolcengineTTS(app_id="x", token="y").synthesize("hi")


@respx.mock
async def test_volcengine_tts_pcm_wrapped_into_wav():
    from dsdaughter.media import VolcengineTTS

    pcm = b"\x00\x01" * 2400  # 0.1s 24k 16bit
    respx.post("https://openspeech.bytedance.com/api/v1/tts").mock(
        return_value=httpx.Response(
            200, json={"code": 3000, "data": base64.b64encode(pcm).decode()}
        )
    )
    audio, rate = await VolcengineTTS(app_id="x", token="y", encoding="pcm").synthesize("hi")
    assert audio.startswith(b"RIFF")
    assert wav_info(audio)[0] == 24000


def test_build_tts_falls_back_to_deepseek(monkeypatch):
    from dsdaughter.media import build_tts

    s = settings(
        tts_provider="volcengine",
        volc_app_id="",
        volc_token="tok",
        deepseek_api_key="sk-test",
    )
    provider = build_tts(s)
    assert provider is not None
    assert provider.name == "deepseek"


def test_build_tts_volcengine_when_configured():
    from dsdaughter.media import build_tts

    s = settings(tts_provider="volcengine", volc_app_id="app", volc_token="tok")
    provider = build_tts(s)
    assert provider is not None
    assert provider.name == "volcengine"


# ----------------------------------------------------- 火山 v3（新版控制台）
@respx.mock
async def test_volcengine_v3_streaming_success():
    from dsdaughter.media import VolcengineTTS

    wav = make_wav(0.3)
    lines = [
        json.dumps({"code": 0, "message": "", "data": base64.b64encode(wav[:44]).decode()}),
        json.dumps({"code": 0, "message": "", "data": base64.b64encode(wav[44:]).decode()}),
    ]
    route = respx.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional").mock(
        return_value=httpx.Response(200, content=("\n".join(lines) + "\n").encode())
    )
    tts = VolcengineTTS(api_key="new-key", voice_type="ICL_uranus_zh_female_bingruoshaonv_tob")
    audio, rate = await tts.synthesize("你好呀")
    assert audio == wav
    assert rate == 24000
    request = route.calls[0].request
    assert request.headers["X-Api-Key"] == "new-key"
    assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    body = json.loads(request.content)
    assert body["req_params"]["speaker"] == "ICL_uranus_zh_female_bingruoshaonv_tob"


@respx.mock
async def test_volcengine_v3_error_code():
    from dsdaughter.media import VolcengineTTS

    respx.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional").mock(
        return_value=httpx.Response(
            200, content=b'{"code":55000000,"message":"resource ID is mismatched"}\n'
        )
    )
    with pytest.raises(VoiceError):
        await VolcengineTTS(api_key="k", voice_type="x").synthesize("hi")


@respx.mock
async def test_volcengine_v3_empty_stream():
    from dsdaughter.media import VolcengineTTS

    respx.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional").mock(
        return_value=httpx.Response(200, content=b"")
    )
    with pytest.raises(VoiceError):
        await VolcengineTTS(api_key="k", voice_type="x").synthesize("hi")


def test_build_tts_prefers_new_console_api_key():
    from dsdaughter.media import build_tts

    s = settings(
        tts_provider="volcengine",
        volc_api_key="new-key",
        volc_app_id="1308532747",
        volc_token="tok",
        volc_voice_type="ICL_uranus_zh_female_bingruoshaonv_tob",
    )
    provider = build_tts(s)
    assert provider is not None
    assert provider.name == "volcengine"
    assert provider._api_key == "new-key"  # noqa: SLF001
