"""视觉：她把截图交给多模态模型。"""

from __future__ import annotations

import io
import json

import httpx
import pytest
import respx
from PIL import Image

from dsdaughter.config import Settings
from dsdaughter.vision import VisionClient, VisionError, build_vision, prepare_image

URL = "https://api.deepseek.com/chat/completions"


def png(size=(200, 400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def settings(**overrides) -> Settings:
    base = {"vision_enabled": True, "deepseek_api_key": "sk-test", "memory_extract": False}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_prepare_image_compresses():
    jpeg = prepare_image(png((2000, 4000)), max_side=720, quality=80)
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    assert image.width <= 720
    assert image.height <= 2160


@respx.mock
async def test_vision_describe():
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "这是设置页面，显示无线调试已开启。"}}]},
        )
    )
    client = VisionClient(base_url="https://api.deepseek.com", api_key="sk-test")
    text = await client.describe(png(), "你看到了什么？")
    assert "设置页面" in text
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "deepseek-flash"
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@respx.mock
async def test_vision_error():
    respx.post(URL).mock(return_value=httpx.Response(400, text="bad image"))
    with pytest.raises(VisionError):
        await VisionClient(base_url="https://api.deepseek.com", api_key="k").describe(png())


def test_build_vision_disabled():
    assert build_vision(settings(vision_enabled=False)) is None
    assert build_vision(settings(deepseek_api_key=None)) is None
    assert build_vision(settings()) is not None
