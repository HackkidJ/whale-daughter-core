"""首页文档页：/project、/plugins-guide、/api/plugins。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dsdaughter.agent import Agent
from dsdaughter.api import create_router
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona

REPO = Path(__file__).resolve().parents[1]


def make_client(*, plugins_enabled: bool = False) -> TestClient:
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        tools_enabled=False,
        plugins_enabled=plugins_enabled,
        plugins_dir=str(REPO / "plugins"),
        docs_dir=str(REPO / "docs"),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(REPO / "persona" / "persona.yaml"),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯"),
    )
    app = FastAPI()
    app.include_router(create_router(agent))
    app.state.mcp = None
    return TestClient(app)


def test_project_page_renders_markdown():
    with make_client() as client:
        response = client.get("/project")
    assert response.status_code == 200
    assert "src/dsdaughter" in response.text
    assert "<table>" in response.text  # markdown 表格被渲染


def test_plugins_guide_page():
    with make_client() as client:
        response = client.get("/plugins-guide")
    assert response.status_code == 200
    assert "plugin.yaml" in response.text
    assert "MCP" in response.text


def test_plugins_status_without_plugins():
    with make_client() as client:
        body = client.get("/api/plugins").json()
    assert body["plugins"] is None
    assert body["mcp"] == {"enabled": False, "servers": []}


def test_plugins_status_lists_weather_demo():
    with make_client(plugins_enabled=True) as client:
        body = client.get("/api/plugins").json()
    names = [item["name"] for item in body["plugins"]["loaded"]]
    assert "weather_demo" in names
    weather = next(item for item in body["plugins"]["loaded"] if item["name"] == "weather_demo")
    assert "get_weather" in weather["tools"]


def test_missing_doc_is_404(tmp_path):
    settings = Settings(
        _env_file=None,
        memory_extract=False,
        tools_enabled=False,
        plugins_enabled=False,
        docs_dir=str(tmp_path / "empty-docs"),
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(REPO / "persona" / "persona.yaml"),
        memory=InMemoryMemoryStore(max_turns=2),
        llm=FakeLLM(reply="嗯"),
    )
    app = FastAPI()
    app.include_router(create_router(agent))
    app.state.mcp = None
    with TestClient(app) as client:
        assert client.get("/project").status_code == 404
