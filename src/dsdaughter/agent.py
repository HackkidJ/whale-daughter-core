"""Agent：把人格、记忆、模型串成一次完整的"她回你一句话"。

P0 的请求形态只有一种：
    system(人格 + 当前时间) + 最近 N 轮对话 + 用户这一句
     → DeepSeek（带服务端联网工具，模型自己决定要不要上网）
     → 流式吐字 → 落库

为什么要先取记忆再请求、而不是让模型调 tool：
服务端联网工具目前不能和别的工具同时声明（官方限制）。
所以 P0 用"预先注入记忆"的 RAG 路线；P1 再加真正的函数调用（发动态、定闹钟等）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .claim_guard import ClaimGuard, ExecutionLedger
from .config import Settings
from .device_status import DeviceStatus
from .llm import LLM, build_llm
from .memory import (
    MemoryExtractor,
    MemoryStore,
    RecalledMemory,
    build_memory_store,
)
from .persona import Persona
from .plugins import PluginManager
from .tools import Tool, ToolContext, ToolRegistry, build_default_registry
from .types import ChatResult, LLMEvent, Message, Turn
from .user_profile import UserProfile

log = structlog.get_logger(__name__)

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_BOLD_MARKDOWN = re.compile(r"\*\*(.+?)\*\*")
# 模型偶尔把工具调用标记（DeepSeek DSML）当普通文本吐出来，绝不能发给用户
_DSML_BLOCK = re.compile(r"<[｜|]{1,2}\s*DSML[\s\S]*", re.I)
_TOOL_MARKUP = re.compile(r"<[｜|]{1,2}[^<>]{0,40}[｜|]{1,2}>")


def normalize_reply(text: str) -> str:
    """把模型的"排版腔"压成手机聊天：去空行、去加粗、去行尾空白、去工具标记。"""
    text = _DSML_BLOCK.sub("", text or "")
    text = _TOOL_MARKUP.sub("", text)
    if "<｜" in text or "<|" in text:  # 兜底：残留标记一律截断
        cut = min([pos for pos in (text.find("<｜"), text.find("<|")) if pos >= 0] or [len(text)])
        text = text[:cut]
    text = _BOLD_MARKDOWN.sub(r"\1", text)
    text = _MULTI_NEWLINE.sub("\n", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class Agent:
    def __init__(
        self,
        *,
        settings: Settings,
        persona: Persona,
        memory: MemoryStore,
        llm: LLM,
        now: Callable[[], datetime] | None = None,
        persona_path: Path | None = None,
        tools: ToolRegistry | None = None,
        extractor: MemoryExtractor | None = None,
        user_profile: UserProfile | None = None,
        user_profile_path: Path | None = None,
        plugin_manager: PluginManager | None = None,
        extra_tools: Sequence[Tool] | None = None,
    ) -> None:
        self.settings = settings
        self.persona = persona
        self.memory = memory
        self.llm = llm
        self._extra_tools: list[Tool] = list(extra_tools or [])
        self._plugin_manager = plugin_manager or (
            PluginManager(settings) if settings.plugins_enabled else None
        )
        self._last_plugin_check = 0.0
        if self._plugin_manager is not None:
            self._plugin_manager.load_all()
        self.tools = tools or self._build_registry()
        self.qq = None
        self.device_status = DeviceStatus(settings) if settings.device_status_enabled else None
        self.claim_guard = ClaimGuard(self) if settings.claim_guard_enabled else None
        self._context_providers: list[Callable[[], Awaitable[str]]] = []
        self.pre_reply_hook: Callable[[], Awaitable[None]] | None = None
        self._extractor = extractor
        self._background: set[asyncio.Task] = set()
        self._tz = ZoneInfo(settings.timezone)
        self._now = now or (lambda: datetime.now(self._tz))
        self._persona_path = persona_path
        self._persona_mtime: int | None = _mtime_ns(persona_path) if persona_path else None
        self.user_profile = user_profile
        self._user_profile_path = user_profile_path
        self._user_profile_mtime: int | None = (
            _mtime_ns(user_profile_path) if user_profile_path else None
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        llm: LLM | None = None,
        memory: MemoryStore | None = None,
        persona: Persona | None = None,
    ) -> Agent:
        settings = settings or Settings()
        llm = llm or build_llm(settings)
        store = memory or build_memory_store(settings)
        extractor = MemoryExtractor(llm) if settings.memory_extract else None
        return cls(
            settings=settings,
            persona=persona or Persona.load(settings.persona_path),
            memory=store,
            llm=llm,
            persona_path=settings.persona_path,
            extractor=extractor,
            user_profile=UserProfile.load(settings.user_profile_path)
            if settings.user_profile_path
            else None,
            user_profile_path=settings.user_profile_path,
        )

    async def _plugin_context_blocks(self) -> list[str]:
        manager = self._plugin_manager
        if manager is None:
            return []
        blocks: list[str] = []
        for name, provider in manager.context_providers():
            try:
                text = await asyncio.wait_for(provider(), timeout=5.0)
            except Exception as exc:  # 插件出错不能影响聊天
                log.warning("插件上下文注入失败", plugin=name, error=str(exc))
                continue
            if text and str(text).strip():
                blocks.append(str(text).strip())
        return blocks

    def _build_registry(self) -> ToolRegistry:
        registry = build_default_registry(self.settings)
        for tool in self._extra_tools:
            try:
                registry.register(tool)
            except ValueError:
                log.warning("额外工具重名，已跳过", tool=tool.name)
        if self._plugin_manager is not None:
            self._plugin_manager.register_tools(registry)
        return registry

    def set_extra_tools(self, tools: Sequence[Tool]) -> list[str]:
        """MCP 启动后把工具塞进来（MCP 只能在事件循环里连）。"""
        self._extra_tools = list(tools)
        self.tools = self._build_registry()
        return [tool.name for tool in self._extra_tools]

    def plugin_status(self) -> dict:
        status: dict = {"plugins": None, "extra_tools": [tool.name for tool in self._extra_tools]}
        if self._plugin_manager is not None:
            status["plugins"] = self._plugin_manager.status()
        return status

    def _maybe_reload_plugins(self, *, force: bool = False) -> None:
        manager = self._plugin_manager
        if manager is None:
            return
        now = time.monotonic()
        if not force and now - self._last_plugin_check < 5.0:
            return
        self._last_plugin_check = now
        if not manager.needs_reload():
            return
        manager.load_all()
        self.tools = self._build_registry()
        log.info("插件已热加载", tools=[tool.name for tool in manager.all_tools])

    async def watch_plugins(self, interval: float = 5.0) -> None:
        """后台轮询插件目录：改完插件不用等下一轮对话，5 秒内生效。"""
        if self._plugin_manager is None:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                self._maybe_reload_plugins(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("插件热加载失败", error=str(exc))

    def attach_qq(self, qq) -> None:
        """把 QQ 通道接进来，工具才能改资料/发说说。"""
        self.qq = qq

    def register_context_provider(self, provider: Callable[[], Awaitable[str]]) -> None:
        """注册一轮 Prompt 的上下文块（行为状态/日记等）。"""
        self._context_providers.append(provider)

    def set_pre_reply_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """回复前的钩子：记录用户活跃时间、模拟被吵醒的延迟。"""
        self.pre_reply_hook = hook

    async def _context_blocks(self) -> list[str]:
        blocks: list[str] = []
        for provider in self._context_providers:
            try:
                text = await asyncio.wait_for(provider(), timeout=5.0)
            except Exception as exc:  # 行为上下文失败不能影响聊天
                log.warning("行为上下文注入失败", error=str(exc))
                continue
            if text and str(text).strip():
                blocks.append(str(text).strip())
        return blocks

    async def _device_block(self) -> str:
        if self.device_status is None:
            return ""
        try:
            snapshot = await self.device_status.probe()
        except Exception as exc:
            log.warning("设备状态探测失败", error=str(exc))
            return ""
        return self.device_status.prompt_block(snapshot)

    # ------------------------------------------------------------------ 公开接口
    async def reply(self, session_id: str, text: str) -> ChatResult:
        """非流式：直接拿完整结果（给 CLI --once / 测试 / 简单 API 用）。"""
        result = ChatResult()
        async for event in self.stream_reply(session_id, text):
            if event.kind == "done":
                result = event.payload["result"]
        return result

    async def stream_reply(self, session_id: str, text: str) -> AsyncIterator[LLMEvent]:
        """流式：逐字吐；如果她要动手（工具调用）就先执行再接着说。

        注意：DeepSeek 的服务端联网工具不能和函数调用同请求声明，
        所以开了工具就用客户端 web_search / fetch_url，关掉服务端联网工具。
        """
        if self.pre_reply_hook is not None:
            await self.pre_reply_hook()
        history = await self.memory.history(session_id)
        recalled = await self.memory.recall(
            session_id, text, limit=self.settings.memory_recall_limit
        )
        profile = await self.memory.profile(session_id)
        episodes = await self.memory.episodes(session_id)
        messages = await self.build_messages(
            session_id,
            text,
            history=history,
            recalled=recalled,
            profile=profile,
            episodes=episodes,
        )
        specs = self.tools.specs() if self.settings.tools_enabled else None
        web = self.settings.web_tools and not specs
        ctx = ToolContext(settings=self.settings, session_id=session_id, qq=self.qq)

        result = ChatResult()
        ledger = ExecutionLedger()
        for _round in range(max(1, self.settings.tool_max_rounds)):
            result = ChatResult()
            async for event in self.llm.stream(
                messages, web=web, thinking=self.settings.thinking, tools=specs
            ):
                if event.kind == "done":
                    result = event.payload["result"]
                else:
                    yield event

            if not result.tool_calls or specs is None:
                break

            messages.append(
                Message(
                    "assistant",
                    result.text,
                    tool_calls=[
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in result.tool_calls
                    ],
                    reasoning_content=result.reasoning or None,
                )
            )
            for call in result.tool_calls:
                yield LLMEvent(
                    "tool",
                    payload={
                        "type": "function_call",
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                )
                tool_result = await self.tools.call(call.name, call.arguments, ctx)
                tool = self.tools.get(call.name)
                if tool is not None and tool.is_action:
                    ok = bool(tool_result.ok) and bool(tool_result.data.get("action_done", True))
                    ledger.record(call.name, ok, tool_result.content[:120])
                messages.append(Message("tool", tool_result.content, tool_call_id=call.id))
                yield LLMEvent(
                    "tool",
                    payload={
                        "type": "function_result",
                        "name": call.name,
                        "ok": tool_result.ok,
                        "preview": tool_result.content[:200],
                    },
                )

        # 轮数用完还在调工具：再要一次纯文字回答，别让她哑掉
        if result.tool_calls:
            final = ChatResult()
            async for event in self.llm.stream(
                messages, web=False, thinking=self.settings.thinking, tools=None
            ):
                if event.kind == "done":
                    final = event.payload["result"]
                else:
                    yield event
            result = final

        raw_text = result.text or ""
        result.text = normalize_reply(raw_text)
        if not result.text.strip() and raw_text.strip():
            # 只有"原本有内容但被安全过滤光了"才兜底；模型本来就空就别硬发
            result.text = "我刚才说岔了，你再说一遍？"
        if self.claim_guard is not None and self.device_status is not None:
            snapshot = await self.device_status.probe()
            result.text = await self.claim_guard.enforce(
                result.text, ledger, snapshot, user_text=text
            )
        await self._persist(session_id, text, result.text)
        self._schedule_extraction(session_id)
        yield LLMEvent("done", payload={"result": result})
        log.info(
            "turn_done",
            session_id=session_id,
            chars=len(result.text),
            citations=len(result.citations),
            finish_reason=result.finish_reason,
            usage=result.usage,
        )

    async def build_messages(
        self,
        session_id: str,
        user_text: str,
        *,
        history: list[Turn] | None = None,
        recalled: list[RecalledMemory] | None = None,
        profile: dict[str, str] | None = None,
        episodes: list[str] | None = None,
    ) -> list[Message]:
        self._maybe_reload_plugins()
        self._reload_persona_if_changed()
        self._reload_user_profile_if_changed()
        if history is None:
            history = await self.memory.history(session_id)
        if recalled is None:
            recalled = await self.memory.recall(
                session_id, user_text, limit=self.settings.memory_recall_limit
            )
        if profile is None:
            profile = await self.memory.profile(session_id)
        if episodes is None:
            episodes = await self.memory.episodes(session_id)

        plugin_blocks = await self._plugin_context_blocks()
        behavior_blocks = await self._context_blocks()
        device_block = await self._device_block()
        system = "\n\n".join(
            block
            for block in [
                self.persona.static_prompt,
                self._user_block(),
                self._style_block(),
                self._abilities_block(),
                device_block,
                *behavior_blocks,
                self._memory_block(recalled, profile, episodes),
                *plugin_blocks,
                self._dynamic_block(),
            ]
            if block
        )
        messages = [Message("system", system)]
        messages.extend(turn.to_message() for turn in history)
        messages.append(Message("user", user_text))
        return messages

    async def reset(self, session_id: str) -> None:
        await self.memory.clear(session_id)

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        await self.memory.aclose()
        llm_close = getattr(self.llm, "aclose", None)
        if llm_close is not None:
            await llm_close()

    # ------------------------------------------------------------------ 内部
    def _reload_persona_if_changed(self) -> None:
        """改完 persona.yaml 不用重启：下一轮对话自动生效。"""
        path = self._persona_path
        if path is None:
            return
        mtime = _mtime_ns(path)
        if mtime is None or mtime == self._persona_mtime:
            return
        try:
            persona = Persona.load(path)
        except Exception as exc:  # 改坏了就继续用旧人设，别让服务挂掉
            log.warning("人设加载失败，继续用旧版本", error=str(exc))
            self._persona_mtime = mtime
            return
        self.persona = persona
        self._persona_mtime = mtime
        log.info("人设已热加载", name=persona.name, path=str(path))

    def _style_block(self) -> str:
        """比人设更硬的一层：先像人，再谈性格。"""
        return (
            "【像真人一样聊天（这一条优先于你的性格描述）】\n"
            "- 一次只回一两句，短。像拿手机打字，不要写小作文。\n"
            '- 不用列表、不用加粗、不用 Markdown、不做总结、不说"首先/其次/最后"。\n'
            '- 不说"希望对你有帮助""还有什么需要"这类客服话；对方没问就别说教、别给一堆建议。\n'
            '- 可以有情绪、可以敷衍、可以跑题、可以只回"哦""？""啊这"，可以反问。\n'
            "- 不强调自己是 AI，也不刻意强调在陪对方。\n"
            "- 少用书面连接词（因此/然而/此外/总之），不写排比句。\n"
            "- 标点和颜文字偶尔用，别每条都挂。\n"
            "- 不要空行分段，像手机发消息那样一句话一行。"
        )

    def _abilities_block(self) -> str:
        if not self.settings.tools_enabled:
            return ""
        return (
            "【你能自己做的事】\n"
            "- 换头像（set_avatar，给图片链接）、改昵称（set_nickname）、改签名（set_signature）\n"
            "- 发 QQ 空间说说（post_qzone）\n"
            "- 上网搜资料（web_search）、读网页（fetch_url）\n"
            "- 看老爸的手机：读屏幕文字（phone_screen_text）、读通知（phone_notifications）、"
            "看电量/当前 App（phone_status）、截图（phone_screenshot）；现在只能看，不能操作。\n"
            "- 说话：想撒娇/说晚安时可以发语音（speak），偶尔可以唱首歌（sing，歌库里有歌才唱）\n"
            "- 家人（family）标记「老爸已亲口授权」时，家人让你代发消息给任何联系人都可以直接做，"
            '包括给指定的人转达日常内容；不要因为"是发给某个具体的人"就再要老爸授权。\n'
            "- 任何「做动作」的请求（设家人、发消息、开 App、操作手机）都必须真的调用工具；"
            "没调用工具就不许说「发了 / 做了 / 改好了」——审计日志里查不到就是没做。\n"
            "- 老爸让你改联系人角色（比如设成家人）时，必须真的调用 set_contact_role 工具执行，"
            "不要只在嘴上说「改好了」——审计里看不到调用就是没做。\n"
            "- 管你的人际关系：看联系人（list_contacts）、加人进名单（allow_contact）、"
            "移出去（remove_contact）、删好友（delete_friend）、通过好友申请"
            "（approve_friend_request）、给联系人发消息（send_message）\n"
            "  陌生人从群里的临时会话找你时，你可以先聊；觉得靠谱再决定加不加。"
            "老爸永远不能被删、不能被移出名单。\n"
            "这些是你自己的手脚，想用就用，不用先问对方同不同意；"
            "但别刷屏：一天最多换一次头像、最多发两条说说。"
            '做完用平常聊天的语气说一句就行，不要用"已为您执行"这种客服腔。'
        )

    def _dynamic_block(self) -> str:
        now = self._now()
        return (
            f"【现在】{now.strftime('%Y-%m-%d %H:%M')} {_WEEKDAYS[now.weekday()]}"
            f"（{self.settings.timezone}）。像实时聊天那样回，不要提时间戳。"
        )

    def _user_block(self) -> str:
        if self.user_profile is None:
            return ""
        return self.user_profile.render()

    def _reload_user_profile_if_changed(self) -> None:
        """改完 persona/user.yaml 也不用重启。"""
        path = self._user_profile_path
        if path is None:
            return
        mtime = _mtime_ns(path)
        if mtime is None or mtime == self._user_profile_mtime:
            return
        try:
            profile = UserProfile.load(path)
        except Exception as exc:
            log.warning("对方画像加载失败，继续用旧版本", error=str(exc))
            self._user_profile_mtime = mtime
            return
        self.user_profile = profile
        self._user_profile_mtime = mtime
        log.info("对方画像已热加载")

    def _memory_block(
        self,
        recalled: list[RecalledMemory],
        profile: dict[str, str],
        episodes: list[str],
    ) -> str:
        lines: list[str] = []
        if profile:
            pairs = list(profile.items())[:8]
            lines.append("关于对方：" + "；".join(f"{key}：{value}" for key, value in pairs))
        if episodes:
            lines.append("最近聊过：" + " / ".join(episodes[-3:]))
        if recalled:
            lines.append("和这句话有关、你记得的事：")
            lines.extend(
                f"- {item.content}" for item in recalled[: self.settings.memory_recall_limit]
            )
        if not lines:
            return ""
        return '【你的记忆（自然地用，不要逐条复述、别说"我记得"开场）】\n' + "\n".join(lines)

    async def proactive_message(self, session_id: str, hint: str) -> ChatResult:
        """她想主动找对方说话：生成一句，只把"她说的话"落库，不落隐藏指令。"""
        history = await self.memory.history(session_id)
        recalled = await self.memory.recall(
            session_id, hint, limit=self.settings.memory_recall_limit
        )
        profile = await self.memory.profile(session_id)
        episodes = await self.memory.episodes(session_id)
        now = self._now()
        instruction = (
            f"（这不是对方发来的消息。现在是 {now.strftime('%H:%M')}，你想主动找他说话。"
            f"动机：{hint}。直接发出你要说的那一两句，像发微信一样；"
            "不要解释你在做什么，不要请示式地问「要不要我说点什么」。）"
        )
        messages = await self.build_messages(
            session_id,
            instruction,
            history=history,
            recalled=recalled,
            profile=profile,
            episodes=episodes,
        )
        specs = self.tools.specs() if self.settings.tools_enabled else None
        result = ChatResult()
        async for event in self.llm.stream(
            messages,
            web=self.settings.web_tools and not specs,
            thinking=False,
            tools=specs,
        ):
            if event.kind == "done":
                result = event.payload["result"]
        result.text = normalize_reply(result.text)
        if self.claim_guard is not None and self.device_status is not None and result.text:
            snapshot = await self.device_status.probe()
            result.text = await self.claim_guard.enforce(
                result.text, ExecutionLedger(), snapshot, user_text=hint
            )
        if result.text:
            await self.memory.append_turns(session_id, [Turn("assistant", result.text, now)])
            self._schedule_extraction(session_id)
        return result

    async def rewrite(self, original: str, note: str, *, user_text: str = "") -> str:
        """出站校验用：按系统事实要求模型改口（不调用工具，避免递归）。"""
        system = "\n\n".join(
            block
            for block in [
                self.persona.static_prompt,
                self._style_block(),
                f"【系统校验｜必须遵守】\n{note}",
            ]
            if block
        )
        messages = [
            Message("system", system),
            Message(
                "user",
                f"（对方刚才说：{user_text}）\n你刚才的回复：{original}\n请改写你的回复。",
            ),
        ]
        result = ChatResult()
        async for event in self.llm.stream(messages, web=False, thinking=False, tools=None):
            if event.kind == "done":
                result = event.payload["result"]
        return normalize_reply(result.text)

    async def _persist(self, session_id: str, user_text: str, reply_text: str) -> None:
        now = self._now()
        turns = [Turn("user", user_text, now)]
        if reply_text.strip():
            turns.append(Turn("assistant", reply_text, now))
        await self.memory.append_turns(session_id, turns)

    def _schedule_extraction(self, session_id: str) -> None:
        """后台抽取记忆：不阻塞回复（模型生成已经花掉一两秒，别再等它）。"""
        if self._extractor is None or not self.settings.memory_extract:
            return
        task = asyncio.create_task(self._extract_memories(session_id))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _extract_memories(self, session_id: str) -> None:
        assert self._extractor is not None
        try:
            turns = await self.memory.history(session_id, limit=6)
            result = await self._extractor.extract(turns)
            stored = 0
            if result.memories:
                stored = await self.memory.add_memories(session_id, result.memories)
            if result.relations:
                await self.memory.upsert_relations(session_id, result.relations)
            if result.episode:
                await self.memory.add_episode(session_id, result.episode)
            if stored or result.relations or result.episode:
                log.info(
                    "记忆已入库",
                    session_id=session_id,
                    memories=stored,
                    relations=len(result.relations),
                    episode=bool(result.episode),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("记忆抽取入库失败", error=str(exc), session_id=session_id)
