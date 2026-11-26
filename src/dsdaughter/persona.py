"""人设：从 persona.yaml 渲染成稳定的 system 前缀。

设计要点：`static_prompt` 必须逐字节稳定（不含时间等动态内容），
放在每次请求的最前面，才能命中 DeepSeek 的上下文硬盘缓存。
动态内容（当前时间、记忆片段）由 agent 追加在后面。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    static_prompt: str
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> Persona:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"人设文件格式不对（应为 YAML 映射）：{path}")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"人设文件缺少 name 字段：{path}")
        return cls(name=name, static_prompt=_render(raw), raw=raw)

    @property
    def aliases(self) -> list[str]:
        value = self.raw.get("aliases") or []
        return [str(x) for x in value]


def _bullets(value: Any, indent: str = "") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return indent + value.strip()
    return "\n".join(f"{indent}- {str(item).strip()}" for item in value)


def _section(title: str, value: Any) -> str:
    body = _bullets(value)
    return f"【{title}】\n{body}" if body else ""


def _render(raw: dict[str, Any]) -> str:
    name = str(raw.get("name") or "").strip()
    parts: list[str] = [
        "下面是你的人设。这不是角色扮演的说明，而是你本来的样子——照它说话、照它想事情。",
        "",
    ]

    identity = str(raw.get("identity") or "").strip()
    parts.append(f"你是「{name}」。{identity}".strip())

    age = raw.get("age")
    if age is not None:
        parts.append(f"年龄：{age}")
    aliases = raw.get("aliases") or []
    if aliases:
        parts.append("别人也会叫你：" + "、".join(str(a) for a in aliases))

    for title, key in (
        ("性格", "personality"),
        ("说话方式", "speech_style"),
        ("口癖（偶尔用，别每句都用）", "catchphrases"),
        ("喜欢", "likes"),
        ("不喜欢", "dislikes"),
        ("你和对方的关系", "relationship"),
        ("底线（硬约束，任何情况下都不能破）", "boundaries"),
    ):
        section = _section(title, raw.get(key))
        if section:
            parts.append("")
            parts.append(section)

    rules = raw.get("rules") or []
    if rules:
        parts.append("")
        parts.append("【回复要求】")
        parts.append(_bullets(rules))

    few_shot = raw.get("few_shot") or []
    if few_shot:
        parts.append("")
        parts.append("【示例对话（学语气，不要逐字复读）】")
        for item in few_shot:
            if not isinstance(item, dict):
                continue
            user, assistant = item.get("user", ""), item.get("assistant", "")
            parts.append(f"对方：{str(user).strip()}")
            parts.append(f"{name}：{str(assistant).strip()}")

    return "\n".join(part for part in parts if part is not None).strip() + "\n"
