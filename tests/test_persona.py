"""人设渲染：稳定、可回滚、不混入动态内容。

测试刻意不写死人设里的具体名字/别名 —— 人设是会频繁改的，
测试应该验证"渲染规则"，而不是"某次人设的具体内容"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dsdaughter.persona import Persona

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"


def _raw() -> dict:
    return yaml.safe_load(PERSONA_PATH.read_text(encoding="utf-8"))


def test_load_persona():
    raw = _raw()
    persona = Persona.load(PERSONA_PATH)
    assert persona.name == raw["name"]
    assert persona.aliases == raw.get("aliases", [])
    assert persona.name in persona.static_prompt


def test_static_prompt_contains_hard_boundaries():
    prompt = Persona.load(PERSONA_PATH).static_prompt
    assert "底线" in prompt
    assert "AI" in prompt
    # 危机干预那条不能丢
    assert "12356" in prompt


def test_static_prompt_is_stable_and_cache_friendly():
    """同样的文件必须渲染出逐字节相同的前缀，否则缓存永远命中不了。"""
    first = Persona.load(PERSONA_PATH).static_prompt
    second = Persona.load(PERSONA_PATH).static_prompt
    assert first == second
    # 动态内容（当前时间）只能由 agent 追加，不能出现在人设前缀里
    assert "【现在】" not in first


def test_missing_persona_raises():
    with pytest.raises(FileNotFoundError):
        Persona.load("/path/does/not/exist.yaml")


def test_invalid_persona_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- 这是一个列表，不是映射\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Persona.load(bad)


def test_persona_without_name_raises(tmp_path):
    bad = tmp_path / "noname.yaml"
    bad.write_text("identity: 一个没有名字的人\n", encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        Persona.load(bad)
