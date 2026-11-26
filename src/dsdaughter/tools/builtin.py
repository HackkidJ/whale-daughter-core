"""内置工具：上网查资料、改自己的资料、换头像、发 QQ 空间说说。

这些都是"她自己的手脚"，所以她可以自己决定用不用 —— 但有配额和冷却，
避免一天换八次头像或者在空间刷屏。
"""

from __future__ import annotations

import base64
import ipaddress
from urllib.parse import urlparse

import httpx
import structlog

from .base import ActionGuard, Tool, ToolContext, ToolError, ToolRegistry, ToolResult
from .contacts import build_contact_tools
from .media import build_media_tools
from .phone import build_phone_tools

log = structlog.get_logger(__name__)

_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_VISIBILITY = {
    1: "所有人可见",
    4: "好友可见",
    16: "部分好友可见",
    64: "仅自己可见",
    128: "部分好友不可见",
}


# --------------------------------------------------------------------- HTTP
def _headers(ctx: ToolContext) -> dict[str, str]:
    key = ctx.settings.deepseek_api_key
    if not key:
        raise ToolError("没有配置 DEEPSEEK_API_KEY，现在上不了网")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _post_json(ctx: ToolContext, path: str, payload: dict) -> dict:
    url = f"{ctx.settings.base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=_headers(ctx))
    except httpx.HTTPError as exc:
        raise ToolError(f"请求失败：{exc}") from exc
    if response.status_code != 200:
        raise ToolError(f"接口返回 {response.status_code}：{response.text[:200]}")
    data = response.json()
    if not isinstance(data, dict):
        raise ToolError("接口返回格式不对")
    return data


def _ensure_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("只支持 http/https 链接")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower()
        if lowered in {"localhost"} or lowered.endswith((".local", ".internal")):
            raise ToolError("不能访问内网地址") from None
        return
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise ToolError("不能访问内网地址")


async def _download_image(url: str) -> bytes:
    _ensure_public_url(url)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ToolError(f"图片下载失败：{exc}") from exc
    if response.status_code != 200:
        raise ToolError(f"图片下载失败：HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise ToolError(f"那个链接不是图片（{content_type or '未知类型'}）")
    data = response.content
    if len(data) > _MAX_IMAGE_BYTES:
        raise ToolError("图片太大了（上限 3MB）")
    return data


# --------------------------------------------------------------------- 上网
async def web_search(ctx: ToolContext, query: str, count: int | None = None) -> ToolResult:
    if not str(query).strip():
        raise ToolError("搜索词不能为空")
    number = max(1, min(int(count or ctx.settings.search_count), 10))
    data = await _post_json(ctx, "/v1/tools/search", {"query": str(query).strip(), "count": number})
    results = data.get("results") or []
    if not results:
        return ToolResult.success("搜了一下，什么也没找到。")
    lines = []
    for item in results:
        summary = item.get("summary") or item.get("snippet") or ""
        lines.append(
            f"- {item.get('title') or '(无标题)'}\n  {item.get('url')}\n  {str(summary)[:400]}"
        )
    return ToolResult.success(
        "搜索结果（回答里带上链接）：\n" + "\n".join(lines), count=len(results)
    )


async def fetch_url(ctx: ToolContext, url: str) -> ToolResult:
    _ensure_public_url(str(url))
    data = await _post_json(ctx, "/v1/tools/fetch", {"url": str(url)})
    content = str(data.get("content") or "")
    if not content.strip():
        raise ToolError("那个页面读不出内容（可能要登录或不是网页）")
    limit = 12000
    truncated = len(content) > limit
    return ToolResult.success(
        f"《{data.get('title') or url}》正文：\n{content[:limit]}"
        + ("\n…（后面太长了，截断）" if truncated else ""),
        truncated=truncated,
    )


# --------------------------------------------------------------------- QQ 资料
async def _qq_call(ctx: ToolContext, action: str, params: dict) -> dict:
    qq = ctx.require_qq()
    try:
        return await qq.server.call(action, params)
    except Exception as exc:
        raise ToolError(f"QQ 那边没成功：{exc}") from exc


async def _fresh_profile(ctx: ToolContext) -> dict:
    """get_login_info 是登录时缓存（昵称可能过期），get_stranger_info 才是服务器上的。"""
    login = (await _qq_call(ctx, "get_login_info", {})).get("data") or {}
    uid = login.get("user_id")
    if uid:
        detail = (await _qq_call(ctx, "get_stranger_info", {"user_id": uid})).get("data") or {}
        if detail.get("nickname"):
            detail.setdefault("user_id", uid)
            return detail
    return login


async def get_self_profile(ctx: ToolContext) -> ToolResult:
    data = await _fresh_profile(ctx)
    nickname = data.get("nickname") or data.get("nick") or "（未知）"
    signature = data.get("long_nick") or data.get("longNick") or ""
    text = f"现在的 QQ 昵称是「{nickname}」，QQ 号 {data.get('user_id')}。"
    if signature:
        text += f"个性签名是「{signature}」。"
    return ToolResult.success(text, nickname=nickname, signature=signature)


async def set_nickname(ctx: ToolContext, nickname: str) -> ToolResult:
    nickname = str(nickname).strip()
    if not 1 <= len(nickname) <= 16:
        raise ToolError("昵称要 1~16 个字")
    await _qq_call(ctx, "set_qq_profile", {"nickname": nickname})
    return ToolResult.success(f"昵称改好了：{nickname}", nickname=nickname)


async def set_signature(ctx: ToolContext, signature: str) -> ToolResult:
    signature = str(signature).strip()
    if not 1 <= len(signature) <= 80:
        raise ToolError("个性签名要 1~80 个字")
    profile = await _fresh_profile(ctx)
    nickname = profile.get("nickname") or ""
    if not nickname:
        raise ToolError("拿不到当前昵称，改不了签名")
    await _qq_call(ctx, "set_qq_profile", {"nickname": nickname, "personal_note": signature})
    return ToolResult.success(f"签名改好了：{signature}", signature=signature)


async def set_qq_avatar(ctx: ToolContext, image_url: str) -> ToolResult:
    image = await _download_image(str(image_url))
    await _qq_call(ctx, "set_qq_avatar", {"file": "base64://" + base64.b64encode(image).decode()})
    return ToolResult.success("头像换好了（对方客户端可能有缓存，过一会儿才显示）")


# --------------------------------------------------------------------- 空间说说
async def post_qzone(
    ctx: ToolContext, content: str, ugc_right: int = 4, image_url: str | None = None
) -> ToolResult:
    text = str(content).strip()
    if not 1 <= len(text) <= 600:
        raise ToolError("说说正文要 1~600 个字")
    visibility = int(ugc_right)
    if visibility not in _VISIBILITY:
        raise ToolError("可见范围只能是 1(所有人)/4(好友)/16/64(仅自己)/128")
    params: dict = {"content": text, "ugc_right": visibility}
    if image_url:
        image = await _download_image(str(image_url))
        params["images"] = ["base64://" + base64.b64encode(image).decode()]
    response = await _qq_call(ctx, "send_qzone_msg", params)
    data = response.get("data") or {}
    tid = data.get("tid") if isinstance(data, dict) else None
    return ToolResult.success(
        f"说说发出去了（{_VISIBILITY[visibility]}）" + (f"，tid={tid}" if tid else ""),
        tid=tid,
        visibility=_VISIBILITY[visibility],
    )


# --------------------------------------------------------------------- 注册
def build_default_registry(settings) -> ToolRegistry:
    registry = ToolRegistry(
        guard=ActionGuard(settings.action_limits, settings.tool_action_cooldown)
    )
    registry.register_all(
        build_contact_tools()
        + build_media_tools()
        + build_phone_tools()
        + [
            Tool(
                name="web_search",
                description="上网搜资料。想知道最新的、你不确定的事，或者对方让你查东西时用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                        "count": {
                            "type": "integer",
                            "description": "返回几条，1-10，默认 5",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["query"],
                },
                handler=web_search,
            ),
            Tool(
                name="fetch_url",
                description="读一个网页的正文。需要看某个链接里具体写了什么时用。",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "网页链接"}},
                    "required": ["url"],
                },
                handler=fetch_url,
            ),
            Tool(
                name="get_self_profile",
                description="看看自己现在的 QQ 昵称和号。改资料前可以先确认。",
                parameters={"type": "object", "properties": {}},
                handler=get_self_profile,
            ),
            Tool(
                name="set_nickname",
                description="改自己的 QQ 昵称（1~16 个字）。想换个名字时用，一天最多 3 次。",
                parameters={
                    "type": "object",
                    "properties": {
                        "nickname": {"type": "string", "description": "新昵称，1-16 字"}
                    },
                    "required": ["nickname"],
                },
                handler=set_nickname,
                is_action=True,
            ),
            Tool(
                name="set_signature",
                description="改自己的 QQ 个性签名（1~80 个字）。像换心情签名一样用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "signature": {"type": "string", "description": "新签名，1-80 字"}
                    },
                    "required": ["signature"],
                },
                handler=set_signature,
                is_action=True,
            ),
            Tool(
                name="set_avatar",
                description=(
                    "换自己的 QQ 头像。给一个图片链接（http/https，<=3MB）。一天最多 1 次。"
                ),
                parameters={
                    "type": "object",
                    "properties": {"image_url": {"type": "string", "description": "图片链接"}},
                    "required": ["image_url"],
                },
                handler=set_qq_avatar,
                is_action=True,
            ),
            Tool(
                name="post_qzone",
                description=(
                    "发一条 QQ 空间说说。想分享心情、发刚看到的图时用。"
                    "可见范围：1 所有人 / 4 好友（默认）/ 64 仅自己。一天最多 2 条。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "正文，1-600 字"},
                        "ugc_right": {
                            "type": "integer",
                            "description": "可见范围：1/4/16/64/128，默认 4",
                            "enum": [1, 4, 16, 64, 128],
                        },
                        "image_url": {
                            "type": "string",
                            "description": "可选，配一张图的链接",
                        },
                    },
                    "required": ["content"],
                },
                handler=post_qzone,
                is_action=True,
            ),
        ]
    )
    return registry
