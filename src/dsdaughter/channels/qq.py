"""QQ 通道：白名单 → 去重 → 调 agent → 拆条发送。

安全默认：只回白名单里的人/群。白名单为空 = 谁都不理（宁可哑，也不要被陌生人调用）。
"""

from __future__ import annotations

import asyncio
import base64
import random
import secrets
import time
from collections import OrderedDict, deque
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from ..agent import Agent
from ..config import Settings
from ..contacts import ContactStore
from ..media.silk import ensure_tencent_silk, wav_to_silk
from ..media.tts import build_tts, wav_info
from ..media.voice_policy import decide_voice
from .onebot import (
    MessageEvent,
    OneBotError,
    OneBotServer,
    Segment,
    at_segment,
    split_for_chat,
    split_text,
    text_segment,
)

log = structlog.get_logger(__name__)

_SEEN_LIMIT = 1024


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for token in (raw or "").replace("，", ",").split(","):
        token = token.strip()
        if token.isdigit():
            ids.add(int(token))
    return ids


class QQChannel:
    def __init__(self, *, agent: Agent, settings: Settings) -> None:
        self.agent = agent
        self.settings = settings
        self.server = OneBotServer(
            token=settings.qq_token, action_timeout=settings.qq_action_timeout
        )
        self.server.on_message = self.handle_message
        self.server.on_request = self.handle_request
        self.pending_requests: dict[str, dict[str, Any]] = {}

        self.contacts = ContactStore(settings)
        self.tts = build_tts(settings)
        self._voice_times: deque[float] = deque(maxlen=100)
        self._last_voice = False
        self.allowed_groups = _parse_ids(settings.qq_allowed_groups)

        self._seen: OrderedDict[int, None] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats = {
            "received": 0,
            "replied": 0,
            "ignored": 0,
            "errors": 0,
            "chunks": 0,
            "requests": 0,
        }

        if not self.contacts.allowed_users and not self.allowed_groups:
            log.warning(
                "QQ 通道没有配置白名单（DSD_QQ_ALLOWED_USERS / DSD_QQ_ALLOWED_GROUPS），"
                "所有消息都会被忽略"
            )

    # ------------------------------------------------------------------ 状态
    @property
    def allowed_users(self) -> set[int]:
        return self.contacts.allowed_users

    def status(self) -> dict[str, Any]:
        return {
            **self.server.status(),
            "persona": self.agent.persona.name,
            "owner": self.contacts.owner,
            "allowed_users": sorted(self.allowed_users),
            "temp_contacts": sorted(self.contacts.temp_contacts()),
            "pending_requests": len(self.pending_requests),
            "allowed_groups": sorted(self.allowed_groups),
            "group_require_at": self.settings.qq_group_require_at,
            **self.stats,
        }

    # ------------------------------------------------------------------ 处理
    async def handle_message(self, event: MessageEvent) -> None:
        self.stats["received"] += 1

        if event.self_id == event.user_id:
            return
        is_temp = self._is_temp_session(event)
        if is_temp:
            self.contacts.remember_temp(event.user_id, event.group_id)
            self.stats["temp_received"] = self.stats.get("temp_received", 0) + 1
        elif not self._permitted(event):
            self.stats["ignored"] += 1
            log.debug("忽略非白名单消息", user_id=event.user_id, group_id=event.group_id)
            return
        if self._duplicate(event.message_id):
            self.stats["ignored"] += 1
            return

        text = event.plain_text()
        if not text:
            return
        if (
            event.is_group
            and self.settings.qq_group_require_at
            and not event.mentioned(self.server.self_id or event.self_id)
        ):
            return

        session_id, prompt = self._session_and_prompt(event, text)
        started = time.monotonic()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                result = await self.agent.reply(session_id, prompt)
            except Exception as exc:
                self.stats["errors"] += 1
                log.error("生成回复失败", error=str(exc), session_id=session_id)
                return

        if await self._maybe_send_voice(event, result.text, session_id, started):
            self.stats["voice_replied"] = self.stats.get("voice_replied", 0) + 1
            log.info(
                "qq 已回复（语音）",
                session_id=session_id,
                chars=len(result.text),
                citations=len(result.citations),
            )
            return

        chunks = self._split_reply(result.text)
        if not chunks:
            return
        self._last_voice = False

        # 拟人节奏：先让她"读一下"、显示正在输入，再按"打字速度"等够时间
        await self._typing_status(event, True)
        target = self._typing_target(result.text)
        elapsed = time.monotonic() - started
        wait = max(0.0, target - elapsed)
        if wait > 0:
            await asyncio.sleep(wait)
        log.info(
            "qq_delay",
            session_id=session_id,
            target=round(target, 2),
            model_seconds=round(elapsed, 2),
            waited=round(wait, 2),
            chunks=len(chunks),
        )

        for index, chunk in enumerate(chunks):
            segments: list[Segment] = [text_segment(chunk)]
            if event.is_group and index == 0:
                segments.insert(0, at_segment(event.user_id))
            try:
                if event.is_group:
                    assert event.group_id is not None
                    await self.server.send_group(event.group_id, segments)
                elif is_temp:
                    group_id = event.group_id or self.contacts.temp_group(event.user_id)
                    await self.server.send_private(event.user_id, segments, group_id=group_id)
                else:
                    await self.server.send_private(event.user_id, segments)
            except OneBotError as exc:
                self.stats["errors"] += 1
                log.warning("发送失败", error=str(exc), session_id=session_id)
                return
            self.stats["replied"] += 1
            self.stats["chunks"] += 1
            if index < len(chunks) - 1:
                await asyncio.sleep(self._chunk_delay(chunk))

        log.info(
            "qq 已回复",
            session_id=session_id,
            chunks=len(chunks),
            chars=len(result.text),
            citations=len(result.citations),
        )

    # ------------------------------------------------------------------ 好友申请
    async def handle_request(self, payload: dict[str, Any]) -> None:
        """好友/加群申请。白名单里的人自动通过 —— 否则小号根本没法加你。"""
        if payload.get("request_type") != "friend":
            return
        self.stats["requests"] += 1
        user_id = int(payload.get("user_id") or 0)
        flag = str(payload.get("flag") or "")
        self.pending_requests[flag] = payload
        if not flag:
            return
        if self.settings.qq_auto_accept_friends and self.contacts.is_allowed(user_id):
            await self.server.call("set_friend_add_request", {"flag": flag, "approve": True})
            self.pending_requests.pop(flag, None)
            log.info("已自动通过白名单好友申请", user_id=user_id)
        else:
            log.info("收到好友申请（未自动通过）", user_id=user_id)

    # ------------------------------------------------------------------ 头像
    async def set_avatar(self, image: bytes) -> dict[str, Any]:
        """改 QQ 头像（OneBot set_qq_avatar，NapCat 支持）。"""
        if not image:
            raise ValueError("图片为空")
        encoded = base64.b64encode(image).decode("ascii")
        return await self.server.call("set_qq_avatar", {"file": f"base64://{encoded}"})

    # ------------------------------------------------------------------ 主动发送
    async def send_text(
        self, *, text: str, user_id: int | None = None, group_id: int | None = None
    ) -> int:
        """主动发消息（P2 主动性、以及运维手动测试用）。返回发出的条数。"""
        chunks = split_text(text, self.settings.qq_reply_max_chars)
        for _index, chunk in enumerate(chunks):
            segments: list[Segment] = [text_segment(chunk)]
            if group_id is not None:
                await self.server.send_group(group_id, segments)
            elif user_id is not None:
                await self.server.send_private(user_id, segments)
            else:
                raise ValueError("user_id / group_id 至少要有一个")
        return len(chunks)

    # ------------------------------------------------------------------ 内部
    def _is_temp_session(self, event: MessageEvent) -> bool:
        """群里点她头像发起的临时会话：不是好友，但可以先聊。"""
        if not self.settings.qq_accept_temp_sessions:
            return False
        if event.is_group or self.contacts.is_allowed(event.user_id):
            return False
        return event.sub_type in {"group", "discuss"} or bool(event.raw.get("temp_session"))

    # ------------------------------------------------------------------ 语音
    def _recent_voice_count(self, window_seconds: float = 3600) -> int:
        now = time.monotonic()
        self._voice_times = deque(
            (stamp for stamp in self._voice_times if now - stamp < window_seconds),
            maxlen=100,
        )
        return len(self._voice_times)

    async def _maybe_send_voice(
        self, event: MessageEvent, text: str, session_id: str, started: float
    ) -> bool:
        if self.tts is None:
            return False
        incoming_voice = any(segment.type == "record" for segment in event.segments)
        decision = decide_voice(
            text,
            settings=self.settings,
            incoming_voice=incoming_voice,
            recent_count=self._recent_voice_count(),
            last_was_voice=self._last_voice,
            hour=self.agent._now().hour,  # noqa: SLF001 - 同一个 agent 的时钟
        )
        if not decision.use:
            return False
        log.info(
            "voice_decision",
            session_id=session_id,
            use=True,
            probability=decision.probability,
            reason=decision.reason,
        )
        await self._typing_status(event, True)
        try:
            wav, _rate = await self.tts.synthesize(text)
        except Exception as exc:
            log.warning("TTS 失败，退回打字", error=str(exc))
            return False
        duration = wav_info(wav)[1]
        # 语音的"拟人节奏"：录音需要时间，按音频时长等一会儿
        wait = max(0.0, min(8.0, duration * 0.6 + 0.3) - (time.monotonic() - started))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            silk = await wav_to_silk(wav)
            await self._send_record_event(event, silk)
        except Exception as exc:
            log.warning("语音发送失败，退回打字", error=str(exc))
            return False
        self._voice_times.append(time.monotonic())
        self._last_voice = True
        self.stats["replied"] += 1
        self.stats["chunks"] += 1
        log.info("qq_voice_sent", session_id=session_id, duration=round(duration, 2))
        return True

    async def _send_record_event(self, event: MessageEvent, silk: bytes) -> None:
        silk = ensure_tencent_silk(silk)
        segment = Segment("record", {"file": "base64://" + base64.b64encode(silk).decode()})
        if event.is_group:
            assert event.group_id is not None
            await self.server.send_group(event.group_id, [segment])
            return
        group_id = None
        if not self.contacts.is_allowed(event.user_id):
            group_id = event.group_id or self.contacts.temp_group(event.user_id)
        await self.server.send_private(event.user_id, [segment], group_id=group_id)

    async def send_voice_to(self, user_id: int, wav: bytes, *, group_id: int | None = None) -> None:
        """工具用：发一条 WAV 语音给某个联系人/临时会话。"""
        silk = await wav_to_silk(wav)
        await self.send_silk_to(user_id, silk, group_id=group_id)

    async def send_silk_to(self, user_id: int, silk: bytes, *, group_id: int | None = None) -> None:
        silk = ensure_tencent_silk(silk)
        segment = Segment("record", {"file": "base64://" + base64.b64encode(silk).decode()})
        await self.server.send_private(user_id, [segment], group_id=group_id)

    # ------------------------------------------------------------------ 对外发送
    async def send_text_to(self, user_id: int, text: str, *, note: str = "") -> int:
        """她主动给某个联系人/临时会话发消息（工具用）。返回发送条数。"""
        chunks = split_text(text, self.settings.qq_reply_max_chars)
        group_id = None
        if not self.contacts.is_allowed(user_id):
            group_id = self.contacts.temp_group(user_id)
        for index, chunk in enumerate(chunks):
            await self.server.send_private(user_id, [text_segment(chunk)], group_id=group_id)
            if index < len(chunks) - 1:
                await asyncio.sleep(self._chunk_delay(chunk))
        log.info("主动发给联系人", user_id=user_id, chunks=len(chunks), note=note)
        return len(chunks)

    def _permitted(self, event: MessageEvent) -> bool:
        if event.is_group:
            return event.group_id in self.allowed_groups
        return self.contacts.is_allowed(event.user_id)

    def _duplicate(self, message_id: int) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return False

    def _session_and_prompt(self, event: MessageEvent, text: str) -> tuple[str, str]:
        if event.is_group:
            session_id = f"qq:g:{event.group_id}:u:{event.user_id}"
            prompt = f"{event.nickname}：{text}" if event.nickname else text
            return session_id, prompt
        if self._is_temp_session(event):
            who = event.nickname or f"QQ{event.user_id}"
            return f"qq:temp:{event.user_id}", f"（临时会话，还不是好友）{who}：{text}"
        session_id = f"qq:u:{event.user_id}"
        role = self.contacts.role_of(event.user_id)
        if role == "family":
            if self.contacts.family_phone_authorized:
                items = []
                for recipient in self.contacts.family_recipients:
                    if self.contacts.family_recipient_any_content(recipient):
                        items.append(f"{recipient}（任意内容，含问候/晚安）")
                    else:
                        items.append(f"{recipient}（仅具体日常转达）")
                allow_text = (
                    "老爸授权过的收件人：" + "、".join(items) + "；只给这些人生效。"
                    if items
                    else "老爸还没指定具体收件人，先让老爸说「授权家人给某某发消息」。"
                )
                prefix = (
                    "（家人身份：老爸已亲口授权家人用手机代发具体日常消息。"
                    f"{allow_text}"
                    "内容要具体、日常（比如转达一句安排/提醒）；不要发「晚安」这类暧昧或没来由的问候；"
                    "消息末尾会注明「鲸鲸代家人转达」。涉及钱、密码、隐私、承诺、骂人的内容仍然先问老爸）"
                )
            else:
                prefix = "（家人身份：老爸还没有授权家人用手机代发，先问一下老爸）"
            return session_id, f"{prefix}{text}"
        return session_id, text

    def _split_reply(self, text: str) -> list[str]:
        """短回复一条发完；长回复按句拆成 2~3 条，像人连发。"""
        if self.settings.qq_split_sentences:
            chunks = split_for_chat(text, max_parts=3)
            if chunks:
                return [
                    c
                    for chunk in chunks
                    for c in split_text(chunk, self.settings.qq_reply_max_chars)
                ]
        return split_text(text, self.settings.qq_reply_max_chars)

    def _typing_target(self, reply: str) -> float:
        """从收到消息到发出第一条的总时长：读 + 打字，封顶 max_typing_delay。"""
        if not self.settings.qq_human_delay:
            return 0.0
        base = random.uniform(self.settings.qq_read_delay_min, self.settings.qq_read_delay_max)
        low = max(0.5, self.settings.qq_typing_cps_min)
        high = max(low + 0.1, self.settings.qq_typing_cps_max)
        cps = random.uniform(low, high)
        return min(base + len(reply) / cps, self.settings.qq_max_typing_delay)

    def _chunk_delay(self, chunk: str) -> float:
        """连发之间的停顿：按这条消息的长度估一下打字时间。"""
        base = max(0.0, self.settings.qq_send_interval)
        if not self.settings.qq_human_delay:
            return base * (0.6 + random.random() * 0.8)
        typing = len(chunk) / max(0.5, random.uniform(8.0, 14.0))
        return min(4.0, max(base * 0.6, typing + random.uniform(0.3, 0.9)))

    async def _typing_status(self, event: MessageEvent, typing: bool) -> None:
        """尽力发一个"正在输入"状态；NapCat 不支持就算了，不影响发消息。"""
        if not self.settings.qq_typing_status:
            return
        params: dict[str, Any] = {"user_id": event.user_id, "event_type": 1 if typing else 2}
        if event.is_group:
            params["group_id"] = event.group_id
        try:
            await self.server.call("set_input_status", params)
        except Exception:
            log.debug("设置输入状态失败（忽略）")


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    user_id: int | None = None
    group_id: int | None = None


class AcceptBody(BaseModel):
    flag: str = Field(min_length=1, max_length=256)


class ActionBody(BaseModel):
    """通用 OneBot 动作调用（运维/调试用，口令保护）。"""

    action: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


def _require_admin(channel: QQChannel, request: Request) -> None:
    """管理接口用 DSD_QQ_TOKEN 当口令；没配 token 就禁用。"""
    token = channel.settings.qq_token
    if not token:
        raise HTTPException(status_code=503, detail="未配置 DSD_QQ_TOKEN，管理接口已禁用")
    provided = request.headers.get("x-admin-token") or ""
    if not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="管理口令不正确")


def create_router(channel: QQChannel) -> APIRouter:
    """OneBot 反向 WS 入口 + 通道状态 + 受保护的管理接口。"""
    router = APIRouter()

    @router.websocket("/onebot/v11/ws")
    async def onebot_websocket(websocket: WebSocket) -> None:
        await channel.server.serve(websocket)

    @router.get("/api/qq/status")
    async def qq_status() -> dict[str, Any]:
        return channel.status()

    @router.post("/api/qq/send")
    async def admin_send(body: SendBody, request: Request) -> dict[str, Any]:
        """主动给她发一条（运维手动测试 / P2 主动消息用）。"""
        _require_admin(channel, request)
        if (body.user_id is None) == (body.group_id is None):
            raise HTTPException(status_code=400, detail="user_id / group_id 必须二选一")
        try:
            chunks = await channel.send_text(
                text=body.text, user_id=body.user_id, group_id=body.group_id
            )
        except OneBotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "chunks": chunks}

    @router.post("/api/qq/avatar")
    async def admin_set_avatar(request: Request) -> dict[str, Any]:
        """上传图片替换机器人 QQ 头像。请求体直接放图片二进制。"""
        _require_admin(channel, request)
        image = await request.body()
        if not image:
            raise HTTPException(status_code=400, detail="请求体为空，直接把图片二进制放进来")
        if len(image) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="图片太大（上限 4MB）")
        content_type = (request.headers.get("content-type") or "").lower()
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Content-Type 需要是 image/*")
        try:
            response = await channel.set_avatar(image)
        except OneBotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "bytes": len(image), "response": response.get("data")}

    @router.post("/api/qq/action")
    async def admin_action(body: ActionBody, request: Request) -> dict[str, Any]:
        """直接调 OneBot 动作，用来探测/调试扩展接口。"""
        _require_admin(channel, request)
        try:
            response = await channel.server.call(body.action, body.params)
        except OneBotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "data": response.get("data"), "retcode": response.get("retcode")}

    @router.get("/api/qq/requests")
    async def list_requests(request: Request) -> dict[str, Any]:
        _require_admin(channel, request)
        return {"pending": list(channel.pending_requests.values())}

    @router.post("/api/qq/accept")
    async def accept_request(body: AcceptBody, request: Request) -> dict[str, Any]:
        _require_admin(channel, request)
        payload = channel.pending_requests.get(body.flag)
        if payload is None:
            raise HTTPException(
                status_code=404, detail="没有这个好友申请（可能已处理或 flag 过期）"
            )
        await channel.server.call("set_friend_add_request", {"flag": body.flag, "approve": True})
        channel.pending_requests.pop(body.flag, None)
        return {"ok": True, "user_id": payload.get("user_id")}

    return router
