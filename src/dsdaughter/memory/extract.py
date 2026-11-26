"""事实抽取：每轮聊完，让 flash 把"以后还用得上"的信息整理成结构化记忆。"""

from __future__ import annotations

import json
import re

import structlog

from ..llm import LLM
from ..types import Message, Turn
from .base import ExtractionResult, MemoryItem, RelationItem

log = structlog.get_logger(__name__)

_SYSTEM = """你是记忆整理器，负责从一段对话里挑出"以后还会用到"的信息，只输出 JSON。

规则：
- 只记关于对方的事实、偏好、重要事件、关系称呼；不记寒暄，不记你自己说过的话，不要编造。
- 最多 5 条记忆；每条 content 用第三人称短句（例："老爸喜欢喝不加糖的美式"）。
- relations 只放稳定信息（称呼、生日、喜好、雷点）；没有就空数组。
- episode 用一句话概括这段对话，没有就空字符串。
- importance 是 0~1 的小数，越长期有用越高。

输出格式：
{"memories":[{"kind":"fact|preference|event|relation","content":"...","keywords":"...","importance":0.7}],
 "relations":[{"key":"称呼","value":"老爸"}],
 "episode":"..."}

没有值得记的就输出 {"memories":[],"relations":[],"episode":""}。"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _format_turns(turns: list[Turn]) -> str:
    lines = []
    for turn in turns:
        speaker = "对方" if turn.role == "user" else "她"
        lines.append(f"{speaker}：{turn.content}")
    return "\n".join(lines)


def parse_extraction(raw: str) -> ExtractionResult:
    match = _JSON_RE.search(raw or "")
    if not match:
        return ExtractionResult()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ExtractionResult()
    if not isinstance(data, dict):
        return ExtractionResult()

    memories: list[MemoryItem] = []
    for item in (data.get("memories") or [])[:5]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        memories.append(
            MemoryItem(
                content=content,
                kind=str(item.get("kind") or "fact"),
                keywords=str(item.get("keywords") or ""),
                importance=float(item.get("importance") or 0.5),
                source="conversation",
            ).normalized()
        )

    relations: list[RelationItem] = []
    for item in (data.get("relations") or [])[:10]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if key and value:
            relations.append(RelationItem(key=key, value=value))

    episode = str(data.get("episode") or "").strip()[:500]
    return ExtractionResult(memories=memories, relations=relations, episode=episode)


class MemoryExtractor:
    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def extract(self, turns: list[Turn]) -> ExtractionResult:
        if not turns:
            return ExtractionResult()
        messages = [
            Message("system", _SYSTEM),
            Message("user", _format_turns(turns)),
        ]
        buffer = ""
        try:
            async for event in self._llm.stream(messages, web=False, thinking=False, tools=None):
                if event.kind == "text":
                    buffer += event.text
        except Exception as exc:  # 抽取失败不能影响正常聊天
            log.warning("记忆抽取失败", error=str(exc))
            return ExtractionResult()
        result = parse_extraction(buffer)
        log.info(
            "记忆抽取完成",
            memories=len(result.memories),
            relations=len(result.relations),
            episode=bool(result.episode),
        )
        return result
