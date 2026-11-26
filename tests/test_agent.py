"""Agent：人格注入、历史拼装、落库、引用透传。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsdaughter.agent import Agent
from dsdaughter.config import Settings
from dsdaughter.llm.fake import FakeLLM
from dsdaughter.memory import InMemoryMemoryStore
from dsdaughter.persona import Persona

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"


def make_agent(*, reply: str = "好呀。", web: bool = True) -> Agent:
    return Agent(
        settings=Settings(
            _env_file=None,
            memory_extract=False,
            web_tools=web,
            history_turns=6,
            tools_enabled=False,
        ),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=6),
        llm=FakeLLM(reply=reply),
    )


async def test_reply_returns_text_and_persists_turns():
    agent = make_agent(reply="我在呢。")
    result = await agent.reply("s1", "你在吗")
    assert result.text == "我在呢。"

    history = await agent.memory.history("s1")
    assert [(t.role, t.content) for t in history] == [
        ("user", "你在吗"),
        ("assistant", "我在呢。"),
    ]


async def test_system_prompt_contains_persona_and_current_time():
    agent = make_agent()
    await agent.reply("s1", "嗨")
    messages = agent.llm.calls[0]
    system = messages[0]
    assert system.role == "system"
    assert agent.persona.name in system.content
    assert "【现在】" in system.content  # 动态信息在系统提示末尾
    assert messages[-1].role == "user"
    assert messages[-1].content == "嗨"


async def test_second_turn_carries_history():
    agent = make_agent()
    await agent.reply("s1", "第一句")
    await agent.reply("s1", "第二句")

    second_call = agent.llm.calls[1]
    assert [m.role for m in second_call] == ["system", "user", "assistant", "user"]
    assert second_call[1].content == "第一句"
    assert second_call[2].content == "好呀。"
    assert second_call[3].content == "第二句"


async def test_sessions_are_isolated():
    agent = make_agent()
    await agent.reply("alice", "我是 alice")
    await agent.reply("bob", "我是 bob")

    bob_call = agent.llm.calls[1]
    assert all("alice" not in m.content for m in bob_call)
    assert [t.content for t in await agent.memory.history("alice")] == ["我是 alice", "好呀。"]


async def test_stream_events_and_citations():
    agent = make_agent(web=True)
    events = [event async for event in agent.stream_reply("s1", "今天有什么新闻")]
    kinds = [event.kind for event in events]

    assert "text" in kinds
    assert "tool" in kinds
    assert kinds[-1] == "done"
    assert events[-1].payload["result"].citations[0].url.startswith("https://")


async def test_web_disabled_means_no_tool_events():
    agent = make_agent(web=False)
    events = [event async for event in agent.stream_reply("s1", "你好")]
    assert all(event.kind != "tool" for event in events)
    assert events[-1].payload["result"].citations == []


async def test_reset_clears_session():
    agent = make_agent()
    await agent.reply("s1", "记住我")
    await agent.reset("s1")
    assert await agent.memory.history("s1") == []


@pytest.mark.parametrize("turns", [1, 6])
async def test_history_never_exceeds_limit(turns):
    agent = Agent.from_settings(
        Settings(_env_file=None, memory_extract=False, history_turns=turns, tools_enabled=False),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=turns),
        llm=FakeLLM(reply="嗯"),
    )
    for index in range(turns * 2):
        await agent.reply("s1", f"第 {index} 句")
    assert len(await agent.memory.history("s1")) <= turns


# ---------------------------------------------------------------- 人设热加载
async def test_persona_hot_reload(tmp_path):
    """改完 persona.yaml 不用重启：下一轮对话自动用新版本。"""
    path = tmp_path / "persona.yaml"
    path.write_text("name: 甲甲\nidentity: 测试用\n", encoding="utf-8")

    agent = Agent(
        settings=Settings(_env_file=None),
        persona=Persona.load(path),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯"),
        persona_path=path,
    )
    assert "甲甲" in (await agent.build_messages("s1", "hi"))[0].content

    path.write_text("name: 乙乙\nidentity: 测试用\n", encoding="utf-8")
    assert "乙乙" in (await agent.build_messages("s1", "hi"))[0].content
    assert agent.persona.name == "乙乙"


async def test_broken_persona_edit_keeps_old_version(tmp_path):
    """人设改坏了不能把服务搞挂：继续用旧版本。"""
    path = tmp_path / "persona.yaml"
    path.write_text("name: 丙丙\nidentity: 测试用\n", encoding="utf-8")

    agent = Agent(
        settings=Settings(_env_file=None),
        persona=Persona.load(path),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯"),
        persona_path=path,
    )
    path.write_text("name: [这不是合法的人设\n", encoding="utf-8")

    content = (await agent.build_messages("s1", "hi"))[0].content
    assert "丙丙" in content


# ---------------------------------------------------------------- 工具调用循环
class _StubQqServer:
    connected = True
    self_id = 1000000002

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action: str, params: dict) -> dict:
        if action == "get_login_info":
            payload = {"nickname": "暮色香草", "user_id": 1000000002}
            return {"status": "ok", "retcode": 0, "data": payload}
        if action == "get_stranger_info":
            payload = {"nickname": "鱼鱼", "long_nick": "今天也想唱给你听。", "user_id": 1000000002}
            return {"status": "ok", "retcode": 0, "data": payload}
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {}}


class _StubQq:
    def __init__(self) -> None:
        self.server = _StubQqServer()


async def test_tool_loop_executes_action_and_feeds_result_back():
    from dsdaughter.types import ToolCall

    settings = Settings(
        _env_file=None,
        memory_extract=False,
        tools_enabled=True,
        tool_action_cooldown=0.0,
        tool_daily_limits="set_nickname:3",
    )
    llm = FakeLLM(
        tool_calls=[ToolCall(id="c1", name="set_nickname", arguments='{"nickname":"鱼鱼"}')],
        reply="改好啦，以后我就叫鱼鱼。",
    )
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=6),
        llm=llm,
    )
    qq = _StubQq()
    agent.attach_qq(qq)

    events = [event async for event in agent.stream_reply("s1", "以后你叫鱼鱼好不好")]
    result = events[-1].payload["result"]

    assert result.text == "改好啦，以后我就叫鱼鱼。"
    assert qq.server.calls == [("set_qq_profile", {"nickname": "鱼鱼"})]

    # 第一次请求要求调工具，第二次请求带上了工具结果
    assert len(llm.calls) == 2
    second_roles = [message.role for message in llm.calls[1]]
    assert "tool" in second_roles
    tool_message = next(message for message in llm.calls[1] if message.role == "tool")
    assert tool_message.tool_call_id == "c1"

    payload_types = [event.payload.get("type") for event in events if event.kind == "tool"]
    assert "function_call" in payload_types
    assert "function_result" in payload_types

    history = await agent.memory.history("s1")
    assert [turn.role for turn in history] == ["user", "assistant"]


async def test_abilities_block_present_when_tools_enabled(tmp_path):
    settings = Settings(_env_file=None, memory_extract=False, tools_enabled=True)
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=2),
        llm=FakeLLM(reply="嗯"),
    )
    system = (await agent.build_messages("s1", "hi"))[0].content
    assert "你能自己做的事" in system
    assert "post_qzone" in system


async def test_abilities_block_absent_when_tools_disabled():
    settings = Settings(_env_file=None, memory_extract=False, tools_enabled=False)
    agent = Agent(
        settings=settings,
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=2),
        llm=FakeLLM(reply="嗯"),
    )
    assert "你能自己做的事" not in (await agent.build_messages("s1", "hi"))[0].content


# ---------------------------------------------------------------- 输出格式归一化
async def test_reply_normalized_no_blank_lines_or_markdown():
    from dsdaughter.agent import normalize_reply

    assert normalize_reply("第一句。\n\n第二句。") == "第一句。\n第二句。"
    assert normalize_reply("**重点**在这") == "重点在这"
    assert normalize_reply("  \n\n 好  \n") == "好"
    assert normalize_reply("") == ""


# ---------------------------------------------------------------- 对方画像 / 主动消息
async def test_user_profile_injected_into_prompt():
    from dsdaughter.user_profile import UserProfile

    profile = UserProfile.load(PERSONA_PATH.parent / "user.yaml")
    assert profile is not None
    agent = Agent(
        settings=Settings(_env_file=None, memory_extract=False, tools_enabled=False),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=4),
        llm=FakeLLM(reply="嗯"),
        user_profile=profile,
    )
    system = (await agent.build_messages("s1", "hi"))[0].content
    assert "22 岁" in system
    assert "计算机专业毕业" in system
    assert "早十点上班" in system
    assert "忧郁" in system
    assert "文艺" in system


async def test_proactive_message_persists_only_her_reply():
    agent = Agent(
        settings=Settings(_env_file=None, memory_extract=False, tools_enabled=False),
        persona=Persona.load(PERSONA_PATH),
        memory=InMemoryMemoryStore(max_turns=6),
        llm=FakeLLM(reply="下班了吧？今天累不累。"),
    )
    result = await agent.proactive_message("qq:u:123", "他刚下班")
    assert result.text == "下班了吧？今天累不累。"

    history = await agent.memory.history("qq:u:123")
    assert [turn.role for turn in history] == ["assistant"]
    assert history[0].content == "下班了吧？今天累不累。"


def test_normalize_reply_strips_dsml():
    from dsdaughter.agent import normalize_reply

    raw = (
        '好的<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="phone_tap">'
        '\n{"x":1}</｜｜DSML｜｜ invoke>'
    )
    assert "DSML" not in normalize_reply(raw)
    assert "phone_tap" not in normalize_reply(raw)
