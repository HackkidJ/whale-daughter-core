"""配置：Key 接受两种变量名，其余走 DSD_ 前缀。"""

from __future__ import annotations

from dsdaughter.config import Settings


def test_api_key_accepts_plain_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-plain")
    monkeypatch.delenv("DSD_DEEPSEEK_API_KEY", raising=False)
    assert Settings(_env_file=None).deepseek_api_key == "sk-plain"


def test_api_key_accepts_prefixed_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSD_DEEPSEEK_API_KEY", "sk-prefixed")
    assert Settings(_env_file=None).deepseek_api_key == "sk-prefixed"


def test_prefixed_overrides(monkeypatch):
    monkeypatch.setenv("DSD_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DSD_WEB_TOOLS", "false")
    settings = Settings(_env_file=None)
    assert settings.model == "deepseek-v4-pro"
    assert settings.web_tools is False


def test_defaults():
    settings = Settings(_env_file=None)
    assert settings.llm_mode == "deepseek"
    assert settings.model == "deepseek-flash"
    assert settings.history_turns == 20
    assert settings.timezone == "Asia/Shanghai"


def test_blank_values_are_treated_as_unset(tmp_path, monkeypatch):
    """.env 里 `KEY=` 留空必须能正常启动（否则 cp .env.example .env 就崩）。"""
    for name in ("DEEPSEEK_API_KEY", "DSD_SAFETY_CHECK", "DSD_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=\nDSD_SAFETY_CHECK=\nDSD_MAX_TOKENS=\n", encoding="utf-8")
    settings = Settings(_env_file=str(env_file))
    assert settings.deepseek_api_key is None
    assert settings.safety_check is None
    assert settings.max_tokens is None
