"""事实抽取：JSON 解析 + 假模型端到端。"""

from __future__ import annotations

from datetime import UTC, datetime

from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory.extract import MemoryExtractor, parse_extraction
from dsdaughter.types import Turn

VALID = """
```json
{
  "memories": [
    {
      "kind": "preference",
      "content": "老爸喜欢喝不加糖的美式",
      "keywords": "咖啡 美式",
      "importance": 0.8
    }
  ],
  "relations": [{"key": "称呼", "value": "老爸"}],
  "episode": "聊了咖啡口味。"
}
```
"""


def test_parse_extraction_strips_code_fence():
    result = parse_extraction(VALID)
    assert len(result.memories) == 1
    assert result.memories[0].kind == "preference"
    assert result.memories[0].importance == 0.8
    assert result.relations[0].key == "称呼"
    assert result.episode == "聊了咖啡口味。"


def test_parse_extraction_empty_and_invalid():
    assert parse_extraction("") == parse_extraction("")
    assert len(parse_extraction('{"memories":[],"relations":[],"episode":""}').memories) == 0
    assert len(parse_extraction("模型胡言乱语").memories) == 0
    assert len(parse_extraction('{"memories":[{"content":""}]}').memories) == 0


def test_parse_extraction_clamps_and_limits():
    raw = (
        '{"memories":['
        + ",".join(f'{{"content":"记忆{index}","importance":9}}' for index in range(9))
        + "]}"
    )
    result = parse_extraction(raw)
    assert len(result.memories) == 5
    assert all(item.importance == 1.0 for item in result.memories)


def _turn(role: str, content: str) -> Turn:
    return Turn(role=role, content=content, ts=datetime.now(UTC))


async def test_extractor_uses_llm_and_parses():
    llm = FakeLLM(reply=VALID)
    extractor = MemoryExtractor(llm)
    result = await extractor.extract(
        [_turn("user", "我喜欢喝不加糖的美式"), _turn("assistant", "记住啦")]
    )
    assert result.memories[0].content == "老爸喜欢喝不加糖的美式"
    assert llm.calls  # 真的调了模型


async def test_extractor_handles_empty_turns():
    result = await MemoryExtractor(FakeLLM(reply="")).extract([])
    assert result.memories == []
