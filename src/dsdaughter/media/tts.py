"""语音合成（TTS）：把她说的话变成音频。

设计成 provider 可插拔：
- `deepseek`：DeepSeek 官方 TTS（已有 API Key 就能用，2 个音色，便宜，无克隆）
- `fake`：测试/无声环境
以后要换火山引擎豆包（声音复刻）/ CosyVoice / GPT-SoVITS，只加一个 provider 类即可。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import struct
import uuid
import wave
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)


class VoiceError(RuntimeError):
    """合成失败（内容被审、网络、额度等）。"""


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """返回 (wav 字节, 采样率)。"""
        ...


def wav_info(wav_bytes: bytes) -> tuple[int, float]:
    """(采样率, 时长秒)。兼容流式写出、data 块长度写成 0xFFFFFFFF 的 wav。"""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = handle.getnframes()
    except (wave.Error, EOFError):
        return 24000, max(0.0, (len(wav_bytes) - 44) / (2 * 24000))
    # DeepSeek 的流式 WAV 会把 data 块长度写成 0xFFFFFFFF，wave 报出离谱的帧数
    bogus_frames = frames <= 0 or frames >= 0x7FFFFFFF
    if not bogus_frames:
        seconds = frames / float(rate or 24000)
    else:
        seconds = max(0.0, (len(wav_bytes) - 44)) / float(
            max(1, (rate or 24000) * channels * width)
        )
    return rate, seconds


class DeepSeekTTS:
    """DeepSeek 官方 TTS：POST /v1/tools/tts，返回 24kHz 单声道 16bit WAV。"""

    name = "deepseek"

    def __init__(self, *, base_url: str, api_key: str, voice_id: str = "stella") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._voice_id = voice_id

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        text = (text or "").strip()
        if not text:
            raise VoiceError("空文本")
        if len(text) > 500:
            text = text[:500]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"text": text, "voice_id": self._voice_id, "format": "wav"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/tools/tts", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise VoiceError(f"TTS 连不上：{exc}") from exc
        if response.status_code != 200:
            raise VoiceError(f"TTS 返回 {response.status_code}：{response.text[:200]}")
        audio = response.content
        if len(audio) < 100:
            raise VoiceError("TTS 返回的音频为空")
        rate, _seconds = wav_info(audio)
        return audio, rate


class FakeTTS:
    """测试用：生成一个短促的正弦波 WAV。"""

    name = "fake"

    def __init__(self, seconds: float = 0.4, rate: int = 24000) -> None:
        self._seconds = seconds
        self._rate = rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        await asyncio.sleep(0)
        total = int(self._rate * self._seconds)
        samples = [
            int(6000 * math.sin(2 * math.pi * 440 * index / self._rate)) for index in range(total)
        ]
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self._rate)
            handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        return buffer.getvalue(), self._rate


class VolcengineTTS:
    """火山引擎豆包语音（v1 精品音色 HTTP 接口）。

    需要 AppID + Access Token（控制台「应用管理」），不是单一 API Key。
    请求：POST https://openspeech.bytedance.com/api/v1/tts
    Header：Authorization: Bearer;{token}（火山特有写法，分号）
    返回：JSON {code: 3000, data: base64(音频)}

    注意：v1 只覆盖精品音色；声音复刻 2.0 / 大模型音色要走 v3
    （X-Api-Resource-Id: seed-icl-2.0，speaker_id=custom_speaker_id），后续再接。
    """

    name = "volcengine"
    endpoint = "https://openspeech.bytedance.com/api/v1/tts"

    v3_endpoint = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

    def __init__(
        self,
        *,
        app_id: str = "",
        token: str = "",
        api_key: str = "",
        resource_id: str = "seed-tts-2.0",
        cluster: str = "volcano_tts",
        voice_type: str = "BV700_streaming",
        encoding: str = "wav",
        speech_rate: float = 1.0,
    ) -> None:
        self._app_id = app_id
        self._token = token
        self._api_key = api_key
        self._resource_id = resource_id
        self._cluster = cluster
        self._voice_type = voice_type
        self._encoding = encoding
        self._speech_rate = speech_rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        text = (text or "").strip()
        if not text:
            raise VoiceError("空文本")
        if len(text) > 500:
            text = text[:500]
        if self._api_key:
            return await self._synthesize_v3(text)
        return await self._synthesize_v1(text)

    async def _synthesize_v3(self, text: str) -> tuple[bytes, int]:
        """新版控制台：X-Api-Key + X-Api-Resource-Id，流式返回 JSON 行（base64 音频块）。"""
        body = {
            "user": {"uid": "dsdaughter"},
            "req_params": {
                "text": text,
                "speaker": self._voice_type,
                "audio_params": {"format": self._encoding, "sample_rate": 24000},
            },
        }
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        chunks: list[bytes] = []
        try:
            async with (
                httpx.AsyncClient(timeout=60.0) as client,
                client.stream("POST", self.v3_endpoint, json=body, headers=headers) as response,
            ):
                if response.status_code != 200:
                    detail = (await response.aread())[:300].decode("utf-8", "ignore")
                    raise VoiceError(f"火山 v3 返回 {response.status_code}：{detail}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    code = int(item.get("code") or 0)
                    if code not in {0, 20000000}:
                        raise VoiceError(f"火山 v3 失败 code={code}：{item.get('message') or ''}")
                    encoded = item.get("data")
                    if encoded:
                        chunks.append(base64.b64decode(encoded))
        except httpx.HTTPError as exc:
            raise VoiceError(f"火山 v3 连不上：{exc}") from exc
        if not chunks:
            raise VoiceError("火山 v3 没返回音频")
        return _ensure_wav(b"".join(chunks), self._encoding), 24000

    async def _synthesize_v1(self, text: str) -> tuple[bytes, int]:
        payload = {
            "app": {
                "appid": self._app_id,
                "token": self._token,
                "cluster": self._cluster,
            },
            "user": {"uid": "dsdaughter"},
            "audio": {
                "voice_type": self._voice_type,
                "encoding": self._encoding,
                "rate": 24000,
                "speed_ratio": self._speech_rate,
            },
            "request": {
                "reqid": uuid.uuid4().hex,
                "text": text,
                "operation": "query",
            },
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    self.endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer;{self._token}",
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code in {401, 403}:
                    # 部分版本用 Bearer 空格；重试一次
                    response = await client.post(
                        self.endpoint,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json",
                        },
                    )
        except httpx.HTTPError as exc:
            raise VoiceError(f"火山 TTS 连不上：{exc}") from exc
        if response.status_code != 200:
            raise VoiceError(f"火山 TTS 返回 {response.status_code}：{response.text[:200]}")
        try:
            result = response.json()
        except ValueError as exc:
            raise VoiceError("火山 TTS 返回的不是 JSON") from exc
        code = int(result.get("code") or -1)
        if code not in {0, 3000}:
            raise VoiceError(f"火山 TTS 失败 code={code}：{result.get('message') or ''}")
        encoded = result.get("data")
        if not encoded:
            raise VoiceError("火山 TTS 没有返回音频")
        audio = base64.b64decode(encoded)
        return _ensure_wav(audio, self._encoding), 24000


def _ensure_wav(audio: bytes, encoding: str) -> bytes:
    """火山可能返回 WAV 或裸 PCM；统一成 WAV 给 silk 编码用。"""
    if audio[:4] == b"RIFF":
        return audio
    if encoding.lower() == "wav" and len(audio) > 44:
        return audio
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(audio)
    return buffer.getvalue()


def _deepseek_provider(settings) -> TTSProvider | None:
    if not getattr(settings, "deepseek_api_key", None):
        return None
    return DeepSeekTTS(
        base_url=getattr(settings, "base_url", "https://api.deepseek.com"),
        api_key=settings.deepseek_api_key,
        voice_id=getattr(settings, "tts_voice", "stella"),
    )


def build_tts(settings) -> TTSProvider | None:
    if not getattr(settings, "voice_enabled", False):
        return None
    provider = getattr(settings, "tts_provider", "deepseek")
    if provider == "fake":
        return FakeTTS()
    if provider == "volcengine":
        app_id = str(getattr(settings, "volc_app_id", "") or "").strip()
        token = str(getattr(settings, "volc_token", "") or "").strip()
        api_key = str(getattr(settings, "volc_api_key", "") or "").strip()
        if api_key or (app_id and token):
            return VolcengineTTS(
                app_id=app_id,
                token=token,
                api_key=api_key,
                resource_id=str(getattr(settings, "volc_resource_id", "seed-tts-2.0")),
                cluster=str(getattr(settings, "volc_cluster", "volcano_tts")),
                voice_type=str(getattr(settings, "volc_voice_type", "BV700_streaming")),
                encoding=str(getattr(settings, "volc_encoding", "wav")),
                speech_rate=float(getattr(settings, "volc_speech_rate", 1.0)),
            )
        log.warning("火山语音缺少 APPID 或 TOKEN，退回 DeepSeek TTS")
        return _deepseek_provider(settings)
    return _deepseek_provider(settings)
