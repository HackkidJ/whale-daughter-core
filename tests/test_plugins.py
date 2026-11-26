"""A：本地插件系统 —— 加载、权限、热加载、天气插件端到端。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import respx

from dsdaughter.agent import Agent
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona
from dsdaughter.plugins import PluginManager
from dsdaughter.tools import ToolContext, ToolRegistry

REPO = Path(__file__).resolve().parents[1]


def asyncio_run(coroutine):
    return asyncio.run(coroutine)


def make_settings(**overrides) -> Settings:
    base = {
        "plugins_enabled": True,
        "plugins_dir": str(REPO / "plugins"),
        "memory_extract": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def write_plugin(
    root: Path,
    name: str,
    *,
    permissions: list[str] | None = None,
    enabled: bool = True,
    body: str | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    perms = permissions if permissions is not None else ["network"]
    (directory / "plugin.yaml").write_text(
        f"name: {name}\nversion: 0.0.1\nenabled: {str(enabled).lower()}\n"
        f"permissions: [{', '.join(perms)}]\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(
        body
        or (
            "from dsdaughter.tools import ToolResult\n\n"
            "def register(api):\n"
            "    api.tool(name='ping_tool', description='ping', "
            "parameters={'type': 'object', 'properties': {}}, handler=ping)\n\n"
            "async def ping(ctx, **kwargs):\n"
            "    return ToolResult.success('pong')\n"
        ),
        encoding="utf-8",
    )
    return directory


# --------------------------------------------------------------------- 官方示例
def test_loads_weather_demo():
    manager = PluginManager(make_settings())
    loaded = manager.load_all()

    names = [plugin.manifest.name for plugin in loaded]
    assert "weather_demo" in names
    assert "get_weather" in [tool.name for tool in manager.all_tools]
    status = manager.status()
    assert status["enabled"] is True
    weather = next(item for item in status["loaded"] if item["name"] == "weather_demo")
    assert weather["permissions"] == ["network"]
    assert weather["tools"] == ["get_weather"]


def test_plugin_tools_register_into_registry():
    manager = PluginManager(make_settings())
    manager.load_all()
    registry = ToolRegistry()
    registered = manager.register_tools(registry)

    assert "get_weather" in registered
    assert "get_weather" in registry.names


def test_plugin_tool_name_collision_is_skipped():
    manager = PluginManager(make_settings())
    manager.load_all()
    registry = ToolRegistry()
    taken = manager.all_tools[0]
    registry.register(taken)  # 先占住这个名字
    registered = manager.register_tools(registry)
    assert taken.name not in registered
    assert len(registered) == len(manager.all_tools) - 1


# --------------------------------------------------------------------- 权限
def test_filesystem_permission_requires_allowlist(tmp_path):
    write_plugin(tmp_path, "sneaky", permissions=["filesystem"])
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path)))
    manager.load_all()
    status = manager.status()
    assert status["loaded"] == []
    assert any("高危权限" in item["reason"] for item in status["skipped"])


def test_filesystem_permission_allowed_with_allowlist(tmp_path):
    write_plugin(tmp_path, "trusted", permissions=["filesystem"])
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path), plugins_allowlist="trusted"))
    manager.load_all()
    assert [plugin.manifest.name for plugin in manager.plugins] == ["trusted"]


def test_unknown_permission_rejected(tmp_path):
    write_plugin(tmp_path, "weird", permissions=["nuclear"])
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path)))
    manager.load_all()
    assert manager.plugins == []
    assert any("未知权限" in item["reason"] for item in manager.status()["skipped"])


def test_allowlist_filters_other_plugins(tmp_path):
    write_plugin(tmp_path, "one")
    write_plugin(tmp_path, "two")
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path), plugins_allowlist="one"))
    manager.load_all()
    assert [plugin.manifest.name for plugin in manager.plugins] == ["one"]


def test_disabled_plugin_is_skipped(tmp_path):
    write_plugin(tmp_path, "sleeping", enabled=False)
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path)))
    manager.load_all()
    assert manager.plugins == []
    assert any("enabled=false" in item["reason"] for item in manager.status()["skipped"])


def test_broken_plugin_does_not_crash(tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "plugin.yaml").write_text(
        "name: broken\npermissions: [network]\n", encoding="utf-8"
    )
    (directory / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path)))
    manager.load_all()
    assert manager.plugins == []
    assert any("加载失败" in item["reason"] for item in manager.status()["skipped"])


# --------------------------------------------------------------------- 热加载
def test_needs_reload_after_file_change(tmp_path):
    write_plugin(tmp_path, "hot")
    manager = PluginManager(make_settings(plugins_dir=str(tmp_path)))
    manager.load_all()
    assert manager.needs_reload() is False

    module = tmp_path / "hot" / "__init__.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert manager.needs_reload() is True

    manager.load_all()
    assert manager.needs_reload() is False
    assert "ping_tool" in [tool.name for tool in manager.all_tools]


# --------------------------------------------------------------------- 天气插件
@respx.mock
async def test_weather_plugin_handler():
    respx.get("https://geocoding-api.open-meteo.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "青岛",
                        "admin1": "山东",
                        "latitude": 36.07,
                        "longitude": 120.38,
                    }
                ]
            },
        )
    )
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 6.2,
                    "weather_code": 1,
                    "relative_humidity_2m": 40,
                    "wind_speed_10m": 12,
                },
                "daily": {
                    "time": ["2026-11-20"],
                    "temperature_2m_min": [3],
                    "temperature_2m_max": [9],
                    "weather_code": [2],
                },
            },
        )
    )
    manager = PluginManager(make_settings())
    manager.load_all()
    weather = next(tool for tool in manager.all_tools if tool.name == "get_weather")

    ctx = ToolContext(settings=make_settings(), session_id="test")
    result = await weather.handler(ctx, city="青岛")

    assert result.ok is True
    assert "青岛" in result.content
    assert "6.2" in result.content
    assert "3~9" in result.content


# --------------------------------------------------------------------- 上下文注入
def test_plugin_context_injected_into_system_prompt(tmp_path):
    directory = tmp_path / "context_demo"
    directory.mkdir()
    (directory / "plugin.yaml").write_text(
        "name: context_demo\npermissions: [network]\n", encoding="utf-8"
    )
    (directory / "__init__.py").write_text(
        "def register(api):\n"
        "    async def block():\n"
        "        return '【测试上下文】她现在的肚子很饿'\n"
        "    api.context(block)\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        tools_enabled=False,
        plugins_enabled=True,
        plugins_dir=str(tmp_path),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(REPO / "persona" / "persona.yaml"),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯"),
    )
    system = asyncio_run(agent.build_messages("s1", "hi"))[0].content
    assert "【测试上下文】她现在的肚子很饿" in system
