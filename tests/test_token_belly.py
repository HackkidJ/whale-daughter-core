"""token_belly 插件：余额查询、饥饿分级、上下文注入。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

from dsdaughter.config import Settings
from dsdaughter.tools import ToolContext, ToolError

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugins" / "token_belly" / "__init__.py"
BALANCE_URL = "https://api.deepseek.com/user/balance"


def load_plugin():
    spec = importlib.util.spec_from_file_location("token_belly_under_test", PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reset_cache()
    return module


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-test",
        base_url="https://api.deepseek.com",
        memory_extract=False,
    )


def balance_response(total: float, *, available: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "is_available": available,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": f"{total:.2f}",
                    "granted_balance": "0.00",
                    "topped_up_balance": f"{total:.2f}",
                }
            ],
        },
    )


# --------------------------------------------------------------------- 分级
def test_hunger_levels():
    plugin = load_plugin()
    assert plugin.hunger(60, 90, True)[0] == "吃饱饱"
    assert plugin.hunger(20, 30, True)[0] == "不饿"
    assert plugin.hunger(5, 7.5, True)[0] == "有点饿"
    assert plugin.hunger(2, 3, True)[0] == "很饿"
    assert plugin.hunger(0.5, 0.75, True)[0] == "快饿死了"
    assert plugin.hunger(0, 0, False)[0] == "断粮"


# --------------------------------------------------------------------- 上下文
@respx.mock
async def test_context_reports_hunger():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=balance_response(3.21))
    provider = plugin.make_context_provider(
        make_settings(), {"daily_cost": 1.5, "refresh_seconds": 180}
    )
    text = await provider()

    assert "3.21" in text
    assert "很饿" in text
    assert "消失" in text
    assert "每句话都提钱" in text  # 防伸手要钱的护栏还在
    assert "¥1.50" in text


@respx.mock
async def test_context_uses_cache():
    plugin = load_plugin()
    route = respx.get(BALANCE_URL).mock(return_value=balance_response(10))
    provider = plugin.make_context_provider(
        make_settings(), {"daily_cost": 1.5, "refresh_seconds": 180}
    )
    await provider()
    await provider()
    await provider()
    assert route.call_count == 1  # 3 分钟缓存生效


@respx.mock
async def test_context_empty_when_balance_unavailable():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=httpx.Response(500, text="boom"))
    provider = plugin.make_context_provider(make_settings(), {})
    assert await provider() == ""


@respx.mock
async def test_rich_context_says_not_hungry():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=balance_response(100))
    provider = plugin.make_context_provider(make_settings(), {"daily_cost": 1.0})
    text = await provider()
    assert "吃饱饱" in text
    assert "100.00" in text


# --------------------------------------------------------------------- 工具
@respx.mock
async def test_check_belly_tool():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=balance_response(0.8))
    ctx = ToolContext(settings=make_settings(), session_id="test")
    result = await plugin.check_belly(ctx)

    assert result.ok is True
    assert "0.80" in result.content
    assert result.data["level"] == "快饿死了"
    assert "快饿死了" in result.content


@respx.mock
async def test_check_belly_reports_death():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=balance_response(0, available=False))
    ctx = ToolContext(settings=make_settings(), session_id="test")
    result = await plugin.check_belly(ctx)

    assert result.ok is True
    assert result.data["level"] == "断粮"
    assert "断粮" in result.content


@respx.mock
async def test_check_belly_surfaces_api_errors():
    plugin = load_plugin()
    respx.get(BALANCE_URL).mock(return_value=httpx.Response(401, text="invalid key"))
    ctx = ToolContext(settings=make_settings(), session_id="test")

    with pytest.raises(ToolError):
        await plugin.check_belly(ctx)


async def test_missing_key_is_friendly():
    plugin = load_plugin()
    settings = make_settings().model_copy(update={"deepseek_api_key": None})
    ctx = ToolContext(settings=settings, session_id="test")
    with pytest.raises(ToolError):
        await plugin.check_belly(ctx)
