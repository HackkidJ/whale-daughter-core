"""她的眼睛：把截图交给多模态模型，换回一段"她看见了什么"。

DeepSeek 从 V4.1 flash 起原生支持图片输入；默认用 `deepseek-flash`，
也可以换 `deepseek-v4-flash-vision-exp` 或任何 OpenAI 兼容的多模态端点。
"""

from __future__ import annotations

import base64
import io

import httpx
import structlog
from PIL import Image

log = structlog.get_logger(__name__)


class VisionError(RuntimeError):
    """视觉模型调用失败。"""


def prepare_image(png_bytes: bytes, *, max_side: int = 720, quality: int = 80) -> bytes:
    """压缩成 JPEG：一张图最多 384 tokens，控制尺寸就是省钱。"""
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    image.thumbnail((max_side, max_side * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


class VisionClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "deepseek-flash",
        max_side: int = 720,
        quality: int = 80,
        max_tokens: int = 400,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_side = max_side
        self._quality = quality
        self._max_tokens = max_tokens

    async def ask(
        self, image: bytes, prompt_template: str, *, max_tokens: int | None = None
    ) -> tuple[str, tuple[int, int], tuple[int, int]]:
        """通用视觉问答。prompt 里可用 {image_width}/{image_height}。

        返回 (回答文本, 发送图的尺寸, 原图尺寸)，坐标换算是调用方的事。
        """
        jpeg = prepare_image(image, max_side=self._max_side, quality=self._quality)
        sent = Image.open(io.BytesIO(jpeg)).size
        original = Image.open(io.BytesIO(image)).size
        # 不能用 .format()：prompt 里包含 JSON 示例（{...}），会被当成占位符
        prompt = (
            prompt_template.replace("{image_width}", str(sent[0]))
            .replace("{image_height}", str(sent[1]))
            .replace("{{", "{")
            .replace("}}", "}")
        )
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise VisionError(f"视觉模型连不上：{exc}") from exc
        if response.status_code != 200:
            raise VisionError(f"视觉模型返回 {response.status_code}：{response.text[:200]}")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise VisionError("视觉模型返回格式不对") from exc
        return str(content or "").strip(), sent, original

    async def describe(self, image: bytes, question: str = "") -> str:
        question = (question or "").strip() or "你看到了什么？用一两句话描述，并读出关键文字。"
        jpeg = prepare_image(image, max_side=self._max_side, quality=self._quality)
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise VisionError(f"视觉模型连不上：{exc}") from exc
        if response.status_code != 200:
            raise VisionError(f"视觉模型返回 {response.status_code}：{response.text[:200]}")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise VisionError("视觉模型返回格式不对") from exc
        return str(content or "").strip()


def build_vision(settings) -> VisionClient | None:
    if not getattr(settings, "vision_enabled", False):
        return None
    api_key = getattr(settings, "deepseek_api_key", None)
    if not api_key:
        log.warning("没有 DEEPSEEK_API_KEY，视觉不可用")
        return None
    return VisionClient(
        base_url=getattr(settings, "base_url", "https://api.deepseek.com"),
        api_key=api_key,
        model=getattr(settings, "vision_model", "deepseek-flash"),
        max_side=int(getattr(settings, "vision_max_side", 720)),
        quality=int(getattr(settings, "vision_jpeg_quality", 80)),
        max_tokens=int(getattr(settings, "vision_max_tokens", 400)),
    )
