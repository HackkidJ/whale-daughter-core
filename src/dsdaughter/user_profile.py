"""关于"老爸"的固定画像：她一直知道的事，不进检索、不靠抽取。

和学习到的记忆（memories）分开：这里是稳定事实（年龄/职业/作息/性格），
每次请求都放在 system 里；学到的新东西仍然走 memories 表。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class UserProfile:
    name: str
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> UserProfile | None:
        file = Path(path)
        if not file.exists():
            return None
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"对方画像格式不对（应为 YAML 映射）：{path}")
        name = str(raw.get("name") or "他").strip()
        return cls(name=name, raw=raw)

    def render(self) -> str:
        raw = self.raw
        lines: list[str] = []

        facts: list[str] = []
        if raw.get("age"):
            facts.append(f"{raw['age']} 岁")
        if raw.get("education"):
            facts.append(str(raw["education"]))
        if raw.get("schedule"):
            facts.append(str(raw["schedule"]))
        if facts:
            lines.append("、".join(facts) + "。")

        traits = [str(item).strip() for item in raw.get("personality") or [] if str(item).strip()]
        if traits:
            lines.append("性格：" + "、".join(traits) + "。")

        likes = [str(item).strip() for item in raw.get("likes") or [] if str(item).strip()]
        if likes:
            lines.append("喜欢的东西：" + "、".join(likes) + "。")

        notes = [str(item).strip() for item in raw.get("notes") or [] if str(item).strip()]
        lines.extend(f"- {note}" for note in notes)

        if not lines:
            return ""
        return (
            "【关于老爸（这些是你早就知道的事，别当成新消息去确认）】\n"
            + "\n".join(lines)
            + "\n用这些去理解他的状态：他上班累、周末才歇、偶尔情绪低；别硬灌鸡汤，也别装作没看见。"
        )
