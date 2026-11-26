"""工具层：内置工具、配额、QQ 动作参数。"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from dsdaughter.config import Settings
from dsdaughter.tools import ToolContext, ToolRegistry, build_default_registry


class StubServer:
    connected = True
    self_id = 1000000002

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action: str, params: dict) -> dict:
        if action == "get_login_info":
            stale = {"nickname": "暮色香草", "user_id": 1000000002}
            return {"status": "ok", "retcode": 0, "data": stale}
        if action == "get_stranger_info":
            fresh = {
                "nickname": "鱼鱼",
                "long_nick": "今天也想唱给你听。",
                "user_id": 1000000002,
            }
            return {"status": "ok", "retcode": 0, "data": fresh}
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {}}


class StubQQ:
    def __init__(self) -> None:
        self.server = StubServer()


def make_ctx(*, qq: object | None = None, **overrides) -> ToolContext:
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        deepseek_api_key="sk-test",
        tool_action_cooldown=0.0,
        **overrides,
    )
    return ToolContext(settings=settings, session_id="test", qq=qq)


def make_registry(**overrides) -> ToolRegistry:
    settings = Settings(_env_file=None, memory_extract=False, tool_action_cooldown=0.0, **overrides)
    return build_default_registry(settings)


# --------------------------------------------------------------------- 注册表
async def test_unknown_tool_returns_failure():
    registry = make_registry()
    result = await registry.call("nope", {}, make_ctx())
    assert result.ok is False
    assert "没有叫 nope 的工具" in result.content


async def test_bad_json_arguments():
    registry = make_registry()
    result = await registry.call("web_search", "{not json", make_ctx())
    assert result.ok is False


async def test_history_recorded():
    registry = make_registry()
    await registry.call("nope", {}, make_ctx())
    assert registry.history[-1]["tool"] == "nope"


# --------------------------------------------------------------------- 上网
@respx.mock
async def test_web_search_formats_results():
    respx.post("https://api.deepseek.com/v1/tools/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "DeepSeek V4.1",
                        "url": "https://example.com/a",
                        "summary": "摘要内容",
                    }
                ]
            },
        )
    )
    registry = make_registry()
    result = await registry.call("web_search", {"query": "DeepSeek V4.1"}, make_ctx())
    assert result.ok is True
    assert "https://example.com/a" in result.content
    assert "摘要内容" in result.content


async def test_web_search_without_key_fails():
    registry = make_registry()
    ctx = make_ctx()
    ctx.settings = ctx.settings.model_copy(update={"deepseek_api_key": None})
    result = await registry.call("web_search", {"query": "x"}, ctx)
    assert result.ok is False
    assert "DEEPSEEK_API_KEY" in result.content


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "http://localhost:8000", "http://10.0.0.1/a", "file:///etc/passwd"],
)
async def test_fetch_url_blocks_internal(url):
    registry = make_registry()
    result = await registry.call("fetch_url", {"url": url}, make_ctx())
    assert result.ok is False


# --------------------------------------------------------------------- 资料
async def test_get_self_profile():
    registry = make_registry()
    qq = StubQQ()
    result = await registry.call("get_self_profile", {}, make_ctx(qq=qq))
    assert result.ok is True
    assert "鱼鱼" in result.content
    assert "今天也想唱给你听" in result.content


async def test_set_nickname_calls_qq():
    registry = make_registry()
    qq = StubQQ()
    result = await registry.call("set_nickname", {"nickname": "鱼鱼"}, make_ctx(qq=qq))
    assert result.ok is True
    assert qq.server.calls == [("set_qq_profile", {"nickname": "鱼鱼"})]


async def test_set_nickname_validates_length():
    registry = make_registry()
    result = await registry.call("set_nickname", {"nickname": "x" * 20}, make_ctx(qq=StubQQ()))
    assert result.ok is False
    assert "16" in result.content


async def test_set_signature_keeps_nickname():
    registry = make_registry()
    qq = StubQQ()
    result = await registry.call("set_signature", {"signature": "今天也想唱歌"}, make_ctx(qq=qq))
    assert result.ok is True
    assert qq.server.calls == [
        ("set_qq_profile", {"nickname": "鱼鱼", "personal_note": "今天也想唱歌"})
    ]


async def test_qq_tools_require_connection():
    registry = make_registry()
    result = await registry.call("set_nickname", {"nickname": "鱼鱼"}, make_ctx(qq=None))
    assert result.ok is False
    assert "QQ" in result.content


# --------------------------------------------------------------------- 头像
@respx.mock
async def test_set_avatar_downloads_and_uploads():
    respx.get("https://example.com/avatar.jpg").mock(
        return_value=httpx.Response(
            200, content=b"fake-image-bytes", headers={"content-type": "image/jpeg"}
        )
    )
    registry = make_registry()
    qq = StubQQ()
    result = await registry.call(
        "set_avatar", {"image_url": "https://example.com/avatar.jpg"}, make_ctx(qq=qq)
    )
    assert result.ok is True
    action, params = qq.server.calls[0]
    assert action == "set_qq_avatar"
    assert params["file"].startswith("base64://")
    assert base64.b64decode(params["file"][len("base64://") :]) == b"fake-image-bytes"


# --------------------------------------------------------------------- 说说
async def test_post_qzone_default_visibility():
    registry = make_registry()
    qq = StubQQ()
    result = await registry.call("post_qzone", {"content": "今天唱歌了"}, make_ctx(qq=qq))
    assert result.ok is True
    assert qq.server.calls == [("send_qzone_msg", {"content": "今天唱歌了", "ugc_right": 4})]


async def test_post_qzone_rejects_bad_visibility():
    registry = make_registry()
    result = await registry.call(
        "post_qzone", {"content": "x", "ugc_right": 2}, make_ctx(qq=StubQQ())
    )
    assert result.ok is False


# --------------------------------------------------------------------- 配额
async def test_daily_limit_blocks_second_call():
    registry = make_registry(tool_daily_limits="set_nickname:1")
    qq = StubQQ()
    ctx = make_ctx(qq=qq)
    first = await registry.call("set_nickname", {"nickname": "鱼鱼"}, ctx)
    second = await registry.call("set_nickname", {"nickname": "小鱼"}, ctx)
    assert first.ok is True
    assert second.ok is False
    assert "今天已经用过" in second.content
    assert len(qq.server.calls) == 1


async def test_cooldown_blocks_rapid_repeat():
    registry = make_registry()
    registry._guard._cooldown = 60.0  # noqa: SLF001 - 测试里直接调参
    qq = StubQQ()
    ctx = make_ctx(qq=qq)
    first = await registry.call("set_signature", {"signature": "a"}, ctx)
    second = await registry.call("set_signature", {"signature": "b"}, ctx)
    assert first.ok is True
    assert second.ok is False
