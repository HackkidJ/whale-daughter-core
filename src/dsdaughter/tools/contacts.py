"""人际关系工具：她能自己决定"谁能找她、她找谁"。

这是把"人的权限"交给她：
- 看联系人 / 看谁在加她
- 把聊得来的人加进白名单、把讨厌的移出去
- 删好友（删完对方还能从临时会话找她，被哄回来）
- 主动给联系人发消息
- 通过好友申请
- 尝试主动加好友（QQ 协议端未必开放，失败会如实说）
"""

from __future__ import annotations

import structlog

from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)


def _channel(ctx: ToolContext):
    return ctx.require_qq()


def _requester_id(ctx: ToolContext) -> int | None:
    parts = ctx.session_id.split(":")
    if ctx.session_id.startswith(("qq:u:", "qq:temp:")) and len(parts) >= 3:
        try:
            return int(parts[2])
        except ValueError:
            return None
    if ctx.session_id.startswith("qq:g:") and len(parts) >= 5:
        try:
            return int(parts[4])
        except ValueError:
            return None
    return None


def _valid_id(user_id: object) -> int:
    try:
        value = int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ToolError("QQ 号要是数字") from exc
    if value <= 0:
        raise ToolError("QQ 号不对")
    return value


async def list_contacts(ctx: ToolContext) -> ToolResult:
    channel = _channel(ctx)
    store = channel.contacts
    lines: list[str] = []
    role_name = {"owner": "老爸", "family": "家人", "friend": "朋友"}
    for contact in store.list_contacts():
        role = store.role_of(contact.user_id)
        tag = f"（{role_name.get(role, role)}）"
        note = f" ·{contact.note}" if contact.note else ""
        lines.append(f"- {contact.user_id}{tag}{note}")
    temps = store.temp_contacts()
    if temps:
        lines.append("只在临时会话里聊过的人：" + "、".join(str(uid) for uid in sorted(temps)))
    if channel.pending_requests:
        lines.append(f"还有 {len(channel.pending_requests)} 个好友申请没处理")
    return ToolResult.success("你现在认识的人：\n" + "\n".join(lines))


async def list_pending_requests(ctx: ToolContext) -> ToolResult:
    channel = _channel(ctx)
    if not channel.pending_requests:
        return ToolResult.success("现在没有人加你。")
    lines = []
    for flag, payload in channel.pending_requests.items():
        user_id = payload.get("user_id")
        comment = payload.get("comment") or ""
        lines.append(f"- QQ {user_id}：{comment}".rstrip("：") + f"（flag={flag[:8]}…）")
    return ToolResult.success("待处理的好友申请：\n" + "\n".join(lines))


async def allow_contact(ctx: ToolContext, user_id: int, note: str = "") -> ToolResult:
    channel = _channel(ctx)
    target = _valid_id(user_id)
    if target == channel.contacts.owner:
        raise ToolError("老爸本来就在，不用加")
    if channel.server.self_id and target == channel.server.self_id:
        raise ToolError("那是我自己的号")
    channel.contacts.allow(target, note=str(note or "").strip()[:40], source="她自己加的")
    return ToolResult.success(f"把 {target} 加进联系人名单了，以后他可以直接找你。")


async def authorize_family_phone(ctx: ToolContext) -> ToolResult:
    channel = _channel(ctx)
    requester = _requester_id(ctx)
    if requester != channel.contacts.owner:
        raise ToolError("只有老爸能授权")
    at = channel.contacts.authorize_family_phone()
    return ToolResult.success(
        f"好了，老爸已亲口授权（{at}）：之后家人让我用手机代发普通消息，不用再问老爸；"
        "涉及钱、密码、隐私、承诺的我还是会先问。"
    )


async def authorize_family_recipient(
    ctx: ToolContext, name: str, any_content: bool = False
) -> ToolResult:
    channel = _channel(ctx)
    requester = _requester_id(ctx)
    if requester != channel.contacts.owner:
        raise ToolError("只有老爸能授权")
    try:
        channel.contacts.authorize_family_recipient(name, any_content=any_content)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    scope = "任意内容" if any_content else "具体日常消息"
    return ToolResult.success(
        f"记下了：家人以后可以让我用手机给「{name}」代发{scope}（消息会注明是鲸鲸代家人转达）。"
    )


async def list_family_recipients(ctx: ToolContext) -> ToolResult:
    channel = _channel(ctx)
    names = channel.contacts.family_recipients
    if not names:
        return ToolResult.success("还没有授权家人代发消息的收件人。")
    return ToolResult.success("家人可以代发消息给：" + "、".join(names))


async def set_contact_role(ctx: ToolContext, user_id: int, role: str) -> ToolResult:
    channel = _channel(ctx)
    requester = _requester_id(ctx)
    if requester != channel.contacts.owner:
        raise ToolError("只有老爸能改联系人角色")
    target = _valid_id(user_id)
    try:
        if not channel.contacts.is_allowed(target):
            channel.contacts.allow(target, source="家人授权", role=role)
            return ToolResult.success(f"把 {target} 加进联系人，角色设为 {role} 了。")
        channel.contacts.set_role(target, role)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return ToolResult.success(f"把 {target} 的角色设成 {role} 了。")


async def remove_contact(ctx: ToolContext, user_id: int) -> ToolResult:
    channel = _channel(ctx)
    target = _valid_id(user_id)
    if target == channel.contacts.owner and target != 0:
        raise ToolError("老爸不能被移出白名单")
    removed = channel.contacts.disallow(target)
    if not removed:
        return ToolResult.success(f"{target} 本来就不在名单里。")
    return ToolResult.success(f"把 {target} 移出白名单了，他再发消息你会收不到。")


async def delete_friend(ctx: ToolContext, user_id: int, reason: str = "") -> ToolResult:
    channel = _channel(ctx)
    target = _valid_id(user_id)
    if target == channel.contacts.owner and target != 0:
        raise ToolError("老爸不能删")

    # 先确认真的是好友：NapCat 对不存在的人也会返回成功，别让 tool 撒谎
    friend_ids: set[int] | None = None
    try:
        listing = await channel.server.call("get_friend_list", {})
        rows = listing.get("data") if isinstance(listing, dict) else None
        if isinstance(rows, list):
            friend_ids = {
                int(item.get("user_id"))
                for item in rows
                if isinstance(item, dict) and item.get("user_id") is not None
            }
    except Exception as exc:
        log.debug("好友列表拿不到，跳过校验", error=str(exc))
    if friend_ids is not None and target not in friend_ids:
        raise ToolError(f"{target} 不在你的好友列表里，没什么可删的")

    try:
        await channel.server.call("delete_friend", {"user_id": target})
    except Exception as exc:
        raise ToolError(f"QQ 那边没删掉：{exc}") from exc
    channel.contacts.disallow(target)
    tail = f"（原因：{reason.strip()[:60]}）" if str(reason or "").strip() else ""
    return ToolResult.success(
        f"把 {target} 从好友里删掉了{tail}。他要是还在群里，可以点我头像发临时会话，"
        "到时候聊得来再把他加回来。"
    )


async def approve_friend_request(ctx: ToolContext, user_id: int) -> ToolResult:
    channel = _channel(ctx)
    target = _valid_id(user_id)
    flag = None
    for pending_flag, payload in channel.pending_requests.items():
        if int(payload.get("user_id") or 0) == target:
            flag = pending_flag
            break
    if flag is None:
        raise ToolError(f"没有 {target} 的好友申请（可能已经处理，或者对方没申请过）")
    try:
        await channel.server.call("set_friend_add_request", {"flag": flag, "approve": True})
    except Exception as exc:
        raise ToolError(f"通过申请失败：{exc}") from exc
    channel.pending_requests.pop(flag, None)
    channel.contacts.allow(target, source="好友申请")
    return ToolResult.success(f"通过 {target} 的好友申请了，也把他加进了联系人名单。")


async def request_friend(ctx: ToolContext, user_id: int, verify: str = "") -> ToolResult:
    """主动加好友：协议端如果不支持，就如实告诉她（不假装成功）。"""
    channel = _channel(ctx)
    target = _valid_id(user_id)
    if channel.contacts.is_allowed(target):
        return ToolResult.success(f"{target} 已经在你名单里了。")
    last_error = ""
    for action in ("send_friend_request", "add_friend"):
        try:
            await channel.server.call(
                action, {"user_id": target, "comment": (verify or "").strip()[:50]}
            )
            return ToolResult.success(f"好友申请发给 {target} 了，等他通过。")
        except Exception as exc:
            last_error = str(exc)
    return ToolResult.failure(
        f"QQ 协议端没有开放「主动加好友」的接口（{last_error}）。"
        "你只能等对方发申请，然后通过；或者先从群里的临时会话聊着。"
    )


async def send_message(ctx: ToolContext, user_id: int, text: str) -> ToolResult:
    channel = _channel(ctx)
    target = _valid_id(user_id)
    content = str(text or "").strip()
    if not content:
        raise ToolError("要说什么？")
    if len(content) > 600:
        raise ToolError("太长了，600 字以内")
    if not (channel.contacts.is_allowed(target) or channel.contacts.known_temp(target)):
        raise ToolError("这个人不在你的联系人里，也不在临时会话里；先加进来再说话")
    await channel.send_text_to(target, content)
    return ToolResult.success(f"消息发给 {target} 了。")


def build_contact_tools() -> list[Tool]:
    return [
        Tool(
            name="list_contacts",
            description="看看你现在认识谁（联系人名单、临时会话过的人、待处理好友申请）。",
            parameters={"type": "object", "properties": {}},
            handler=list_contacts,
        ),
        Tool(
            name="list_pending_requests",
            description="看看谁在加你好友、申请留言写了什么。",
            parameters={"type": "object", "properties": {}},
            handler=list_pending_requests,
        ),
        Tool(
            name="allow_contact",
            description="把一个人加进联系人白名单，以后他能直接给你发消息。一天最多 3 个。",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方 QQ 号"},
                    "note": {"type": "string", "description": "备注，比如「群里认识的」"},
                },
                "required": ["user_id"],
            },
            handler=allow_contact,
            is_action=True,
        ),
        Tool(
            name="remove_contact",
            description="把一个人移出白名单，他以后发的消息你会收不到。老爸不能被移出。",
            parameters={
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "对方 QQ 号"}},
                "required": ["user_id"],
            },
            handler=remove_contact,
            is_action=True,
        ),
        Tool(
            name="authorize_family_recipient",
            description=(
                "老爸指定一个联系人（微信/QQ 名字），允许家人让鲸鲸代发具体日常消息给 TA。"
                "只有老爸能调用；家人发消息时必须先有老爸对这个收件人的授权。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "联系人名字，比如「黄佳佳」"}
                },
                "required": ["name"],
            },
            handler=authorize_family_recipient,
            is_action=True,
        ),
        Tool(
            name="list_family_recipients",
            description="看看老爸授权了哪些联系人可以接收家人代发的消息。",
            parameters={"type": "object", "properties": {}},
            handler=list_family_recipients,
        ),
        Tool(
            name="authorize_family_phone",
            description=(
                "老爸亲口授权家人（family）用手机代发普通消息。只需要授权一次，会持久记录；"
                "之后家人代发不需要再问老爸。只有老爸能调用。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=authorize_family_phone,
            is_action=True,
        ),
        Tool(
            name="set_contact_role",
            description=(
                "设置联系人角色：family=家人（可以用手机代发消息），friend=朋友（只能聊天）。"
                "只有老爸能让我改，所以老爸亲口要求时，我直接调用这个工具执行，"
                "不要让他自己动手（他在 QQ 里没有这个按钮）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方 QQ 号"},
                    "role": {"type": "string", "enum": ["family", "friend"]},
                },
                "required": ["user_id", "role"],
            },
            handler=set_contact_role,
            is_action=True,
        ),
        Tool(
            name="delete_friend",
            description=(
                "删掉一个好友。删完他还能从群里点你头像发临时会话，到时候聊得来可以再加回来。"
                "老爸不能删。一天最多 2 次。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方 QQ 号"},
                    "reason": {"type": "string", "description": "为什么删（可选）"},
                },
                "required": ["user_id"],
            },
            handler=delete_friend,
            is_action=True,
        ),
        Tool(
            name="approve_friend_request",
            description="通过某个人的好友申请（同时把他加进联系人名单）。",
            parameters={
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "申请人的 QQ 号"}},
                "required": ["user_id"],
            },
            handler=approve_friend_request,
            is_action=True,
        ),
        Tool(
            name="request_friend",
            description=(
                "尝试主动加某人为好友。注意：QQ 协议端可能没开放这个接口，"
                "失败就如实告诉我，不要假装成功。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方 QQ 号"},
                    "verify": {"type": "string", "description": "验证消息"},
                },
                "required": ["user_id"],
            },
            handler=request_friend,
            is_action=True,
        ),
        Tool(
            name="send_message",
            description="主动给某个联系人（或临时会话聊过的人）发一条消息。一天最多 10 条。",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方 QQ 号"},
                    "text": {"type": "string", "description": "要说的话，600 字以内"},
                },
                "required": ["user_id", "text"],
            },
            handler=send_message,
            is_action=True,
        ),
    ]
