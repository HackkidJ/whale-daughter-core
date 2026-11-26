"""DeepSeek 流式解析：不联网，用 mock 的 SSE 报文验证协议细节。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from dsdaughter.config import Settings
from dsdaughter.llm.base import LLMError
from dsdaughter.llm.deepseek import DeepSeekLLM
from dsdaughter.types import Message

CHAT_URL = "https://api.deepseek.com/chat/completions"


def _sse(*chunks: dict) -> str:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _chunk(
    delta: dict,
    finish_reason: str | None = None,
    *,
    choice_extra: dict | None = None,
    **extra: object,
) -> dict:
    choice: dict = {"index": 0, "delta": delta, "finish_reason": finish_reason}
    choice.update(choice_extra or {})
    chunk = {"choices": [choice]}
    chunk.update(extra)
    return chunk


def _settings(**overrides: object) -> Settings:
    base = {
        "deepseek_api_key": "sk-test",
        "model": "deepseek-flash",
        "web_tools": True,
        "temperature": 0.7,
        "stream_usage": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, memory_extract=False, **base)


async def _collect(
    llm: DeepSeekLLM,
    messages: list[Message],
    *,
    web: bool = True,
    thinking: bool = False,
    tools: list[dict] | None = None,
):
    events = [
        event async for event in llm.stream(messages, web=web, thinking=thinking, tools=tools)
    ]
    await llm.aclose()
    return events


def _route_body() -> str:
    return _sse(
        _chunk(
            {},
            choice_extra={
                "server_tool_event": {
                    "type": "call_started",
                    "step": 0,
                    "call": {
                        "index": 0,
                        "type": "deepseek_web_search",
                        "kind": "search",
                        "action": {"type": "search", "queries": ["今天新闻"]},
                        "status": "in_progress",
                    },
                }
            },
        ),
        _chunk({"reasoning_content": "先想一下。"}),
        _chunk({"content": "你好"}),
        _chunk({"content": "呀！"}),
        _chunk(
            {
                "content": "",
                "annotations": [
                    {
                        "type": "url_citation",
                        "url_citation": {"url": "https://example.com/a", "title": "示例 A"},
                    }
                ],
                "server_tool_calls": [
                    {
                        "index": 0,
                        "reasoning_content": "",
                        "calls": [
                            {
                                "index": 0,
                                "type": "deepseek_web_search",
                                "kind": "search",
                                "status": "completed",
                                "sources": [{"url": "https://example.com/b", "title": "示例 B"}],
                            }
                        ],
                    }
                ],
            },
            finish_reason="stop",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 40,
            },
        ),
    )


@respx.mock
async def test_stream_parses_text_thinking_tools_and_citations():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, text=_route_body(), headers={"content-type": "text/event-stream"}
        )
    )
    llm = DeepSeekLLM(_settings())
    events = await _collect(llm, [Message("user", "今天有什么新闻")])

    kinds = [event.kind for event in events]
    assert kinds == ["tool", "thinking", "text", "text", "done"]

    result = events[-1].payload["result"]
    assert result.text == "你好呀！"
    assert result.reasoning == "先想一下。"
    assert [c.url for c in result.citations] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result.usage["prompt_cache_hit_tokens"] == 60
    assert result.finish_reason == "stop"

    payload = json.loads(route.calls[0].request.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"] == [
        {"type": "deepseek_web_search"},
        {"type": "deepseek_read_url"},
    ]
    assert payload["thinking"] == {"type": "disabled"}  # 默认必须显式关思考
    assert payload["messages"] == [{"role": "user", "content": "今天有什么新闻"}]


@respx.mock
async def test_citations_fall_back_to_tool_sources():
    body = _sse(
        _chunk({"content": "答案"}),
        _chunk(
            {
                "content": "",
                "server_tool_calls": [
                    {
                        "index": 0,
                        "calls": [
                            {
                                "index": 0,
                                "sources": [
                                    {"url": "https://example.com/only-source", "title": "唯一来源"}
                                ],
                            }
                        ],
                    }
                ],
            },
            finish_reason="stop",
        ),
    )
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    llm = DeepSeekLLM(_settings())
    events = await _collect(llm, [Message("user", "hi")])
    assert [c.url for c in events[-1].payload["result"].citations] == [
        "https://example.com/only-source"
    ]


@respx.mock
async def test_thinking_mode_enables_deep_search_and_effort():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=_sse(_chunk({"content": "ok"}, finish_reason="stop")))
    )
    llm = DeepSeekLLM(_settings(thinking=True, reasoning_effort="max"))
    await _collect(llm, [Message("user", "hi")], web=True, thinking=True)

    payload = json.loads(route.calls[0].request.content)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    # 思考模式 + 联网工具 = 深度搜索
    assert payload["tools"] == [
        {"type": "deepseek_web_search"},
        {"type": "deepseek_read_url"},
    ]


@respx.mock
async def test_safety_check_disables_web_tools():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, text=_sse(_chunk({"content": "ok"}, finish_reason="stop")), headers={}
        )
    )
    llm = DeepSeekLLM(_settings(safety_check="high"))
    await _collect(llm, [Message("user", "hi")])

    payload = json.loads(route.calls[0].request.content)
    assert "tools" not in payload
    assert payload["safety_check"] == {"level": "high"}


@respx.mock
async def test_http_error_raises_llm_error():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
    )
    llm = DeepSeekLLM(_settings())
    with pytest.raises(LLMError) as excinfo:
        await _collect(llm, [Message("user", "hi")])
    assert excinfo.value.status_code == 401
    assert "401" in str(excinfo.value)


@respx.mock
async def test_missing_api_key_fails_fast():
    with pytest.raises(LLMError):
        DeepSeekLLM(Settings(_env_file=None, memory_extract=False, deepseek_api_key=None))


@respx.mock
async def test_stream_parses_function_tool_calls():
    body = _sse(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query":'},
                    }
                ]
            }
        ),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"DeepSeek"}'}}]}),
        _chunk({}, finish_reason="tool_calls"),
    )
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    llm = DeepSeekLLM(_settings())
    events = await _collect(llm, [Message("user", "搜一下")], web=False)
    result = events[-1].payload["result"]
    assert result.finish_reason == "tool_calls"
    assert [call.name for call in result.tool_calls] == ["web_search"]
    assert json.loads(result.tool_calls[0].arguments) == {"query": "DeepSeek"}
    assert result.text == ""


@respx.mock
async def test_function_tools_replace_server_web_tools():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=_sse(_chunk({"content": "ok"}, finish_reason="stop")))
    )
    llm = DeepSeekLLM(_settings())
    spec = {
        "type": "function",
        "function": {"name": "web_search", "description": "搜索", "parameters": {"type": "object"}},
    }
    await _collect(llm, [Message("user", "hi")], web=True, tools=[spec])
    payload = json.loads(route.calls[0].request.content)
    assert payload["tools"] == [spec]
    assert {"type": "deepseek_web_search"} not in payload["tools"]
