"""OneBot 11 协议实现：事件解析、消息段、消息拆分、反向 WebSocket 连接。

连接方向是"反向"：NapCat 作为客户端连到我们（bot）的 `/onebot/v11/ws`，
一个连接上同时走事件（NapCat → bot）和动作调用（bot → NapCat），
用 `echo` 字段配对请求与响应。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

log = structlog.get_logger(__name__)

# 非文本消息在对话里显示成什么
SEGMENT_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "mface": "[表情]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
}


class OneBotError(RuntimeError):
    """动作调用失败 / 未连接 / 超时。"""


# --------------------------------------------------------------------- 消息段
@dataclass(slots=True)
class Segment:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Segment:
        return cls(type=str(raw.get("type", "text")), data=dict(raw.get("data") or {}))

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}


def text_segment(text: str) -> Segment:
    return Segment("text", {"text": text})


def at_segment(qq: int | str) -> Segment:
    return Segment("at", {"qq": str(qq)})


_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\]]*)?)\]")


def _unescape(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def parse_segments(raw: Any) -> list[Segment]:
    """优先解析数组格式；兼容 CQ 码字符串（NapCat 两种都可能发）。"""
    if isinstance(raw, list):
        return [Segment.from_dict(item) for item in raw if isinstance(item, dict)]
    if isinstance(raw, str):
        return _parse_cq(raw)
    return []


def _parse_cq(text: str) -> list[Segment]:
    segments: list[Segment] = []
    position = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > position:
            segments.append(text_segment(_unescape(text[position : match.start()])))
        params: dict[str, Any] = {}
        raw_params = match.group(2).lstrip(",")
        if raw_params:
            for pair in raw_params.split(","):
                key, _, value = pair.partition("=")
                if key:
                    params[key] = _unescape(value)
        segments.append(Segment(match.group(1), params))
        position = match.end()
    if position < len(text):
        segments.append(text_segment(_unescape(text[position:])))
    return segments


# --------------------------------------------------------------------- 事件
@dataclass(slots=True)
class MessageEvent:
    message_id: int
    self_id: int
    user_id: int
    nickname: str
    segments: list[Segment]
    group_id: int | None = None
    message_type: str = "private"
    sub_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MessageEvent | None:
        if payload.get("post_type") != "message":
            return None
        message_id = payload.get("message_id")
        if message_id is None:
            return None
        segments = parse_segments(payload.get("message"))
        if not segments and payload.get("raw_message"):
            segments = parse_segments(str(payload["raw_message"]))
        sender = payload.get("sender") or {}
        group_id = payload.get("group_id")
        return cls(
            message_id=int(message_id),
            self_id=int(payload.get("self_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            nickname=str(sender.get("card") or sender.get("nickname") or ""),
            segments=segments,
            group_id=int(group_id) if group_id is not None else None,
            message_type=str(
                payload.get("message_type") or ("group" if group_id is not None else "private")
            ),
            sub_type=str(payload.get("sub_type") or ""),
            raw=payload,
        )

    @property
    def is_group(self) -> bool:
        # 注意：临时会话（private）也可能带 group_id 作为来源群，不能用 group_id 判断
        return self.message_type == "group"

    def plain_text(self) -> str:
        parts: list[str] = []
        for segment in self.segments:
            if segment.type == "text":
                parts.append(str(segment.data.get("text") or ""))
            elif segment.type in SEGMENT_PLACEHOLDERS:
                parts.append(SEGMENT_PLACEHOLDERS[segment.type])
        return "".join(parts).strip()

    def mentioned(self, qq: int | None) -> bool:
        for segment in self.segments:
            if segment.type != "at":
                continue
            target = str(segment.data.get("qq") or "")
            if target == "all" or (qq is not None and target == str(qq)):
                return True
        return False


_SENTENCE_END = re.compile(r"(?<=[。！？!?…\n])\s*")


def split_for_chat(text: str, *, min_chars: int = 60, max_parts: int = 3) -> list[str]:
    """像人一样"连发"：短回复不拆；长回复按句拆成 2~3 条。"""
    text = text.strip()
    if not text:
        return []
    if len(text) < min_chars:
        return [text]

    pieces = [piece.strip() for piece in _SENTENCE_END.split(text) if piece.strip()]
    if len(pieces) <= 1:
        return [text]

    target = min(max_parts, max(2, round(len(text) / 45)))
    per = math.ceil(len(pieces) / target)
    chunks = ["".join(pieces[index : index + per]).strip() for index in range(0, len(pieces), per)]
    return [chunk for chunk in chunks if chunk][:max_parts]


def split_text(text: str, limit: int = 900) -> list[str]:
    """按 QQ 消息长度拆成多条，尽量在标点/换行处断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("，"),
        )
        if cut < limit // 3:
            cut = limit
        else:
            cut += 1
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [chunk for chunk in chunks if chunk]


# --------------------------------------------------------------------- 连接
class OneBotServer:
    """管理 NapCat 的反向 WebSocket 连接，并收发动作调用。"""

    def __init__(self, *, token: str = "", action_timeout: float = 30.0) -> None:
        self.token = token
        self.action_timeout = action_timeout
        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.self_id: int | None = None
        self.last_event_at: float | None = None
        self.connected_since: float | None = None
        self.on_message: Callable[[MessageEvent], Awaitable[None]] | None = None
        self.on_request: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "self_id": self.self_id,
            "last_event_seconds_ago": (
                round(time.monotonic() - self.last_event_at, 1) if self.last_event_at else None
            ),
            "uptime_seconds": (
                round(time.monotonic() - self.connected_since, 1) if self.connected_since else None
            ),
        }

    # ---------------------------------------------------------------- 鉴权
    def _authorized(self, websocket: WebSocket) -> bool:
        if not self.token:
            return True
        header = websocket.headers.get("authorization") or ""
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        query = websocket.query_params.get("access_token", "")
        return self.token in {bearer, query}

    # ---------------------------------------------------------------- 主循环
    async def serve(self, websocket: WebSocket) -> None:
        if not self._authorized(websocket):
            log.warning("OneBot 连接鉴权失败，已拒绝")
            await websocket.close(code=1008)
            return

        await websocket.accept()
        if self._ws is not None:
            log.warning("已有 OneBot 连接，替换为新连接")
        self._ws = websocket
        self.connected_since = time.monotonic()
        log.info("OneBot 已连接")

        try:
            while True:
                payload = await websocket.receive_json()
                if isinstance(payload, dict):
                    self._dispatch(payload)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # 协议异常/连接被掐断
            log.warning("OneBot 连接异常", error=str(exc))
        finally:
            if self._ws is websocket:
                self._ws = None
            self.connected_since = None
            self._fail_pending("OneBot 连接已断开")
            for task in list(self._tasks):
                task.cancel()
            self._tasks.clear()
            log.info("OneBot 已断开")

    def _dispatch(self, payload: dict[str, Any]) -> None:
        echo = payload.get("echo")
        if echo is not None:
            future = self._pending.pop(str(echo), None)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        post_type = payload.get("post_type")
        if post_type == "meta_event":
            self.last_event_at = time.monotonic()
            if payload.get("self_id"):
                self.self_id = int(payload["self_id"])
            return

        if post_type == "request":
            self.last_event_at = time.monotonic()
            if self.on_request is not None:
                task = asyncio.create_task(self._safe_request(payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return

        if post_type in {"message", "message_sent"}:
            self.last_event_at = time.monotonic()
            if post_type == "message_sent":
                return  # 自己发出去的消息回显，忽略
            event = MessageEvent.from_payload(payload)
            if event is None or self.on_message is None:
                return
            task = asyncio.create_task(self._safe_handle(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_request(self, payload: dict[str, Any]) -> None:
        assert self.on_request is not None
        try:
            await self.on_request(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("处理请求事件失败", error=str(exc))

    async def _safe_handle(self, event: MessageEvent) -> None:
        assert self.on_message is not None
        try:
            await self.on_message(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("处理消息失败", error=str(exc), user_id=event.user_id)

    # ---------------------------------------------------------------- 动作
    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        websocket = self._ws
        if websocket is None:
            raise OneBotError("OneBot 未连接")

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        try:
            await websocket.send_json({"action": action, "params": params, "echo": echo})
            response = await asyncio.wait_for(future, timeout=self.action_timeout)
        except TimeoutError as exc:
            raise OneBotError(f"等待 {action} 响应超时") from exc
        finally:
            self._pending.pop(echo, None)

        if response.get("status") == "failed" or int(response.get("retcode") or 0) != 0:
            raise OneBotError(
                f"{action} 失败：retcode={response.get('retcode')} "
                f"{response.get('wording') or response.get('msg') or ''}"
            )
        return response

    async def send_private(
        self, user_id: int, segments: list[Segment], *, group_id: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "user_id": user_id,
            "message": [segment.to_dict() for segment in segments],
        }
        if group_id:
            # 临时会话（还不是好友）需要带上来源群，QQ 才会把消息投递过去
            params["group_id"] = group_id
        return await self.call("send_private_msg", params)

    async def send_group(self, group_id: int, segments: list[Segment]) -> dict[str, Any]:
        return await self.call(
            "send_group_msg",
            {"group_id": group_id, "message": [segment.to_dict() for segment in segments]},
        )

    def _fail_pending(self, reason: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(OneBotError(reason))
        self._pending.clear()
