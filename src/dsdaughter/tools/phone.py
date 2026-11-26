"""手机能力：先只读（看见手机、读通知、看电量），动作类后续逐步开放。"""

from __future__ import annotations

import asyncio

import structlog

from ..phone import ScreenshotStore
from ..phone.client import PhoneClient, PhoneError
from ..phone.peeker import run_taobao_browse
from ..phone.vision_agent import PhoneTaskError, run_phone_task
from ..vision import VisionError, build_vision
from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)


def _session_user_id(session_id: str) -> int | None:
    parts = session_id.split(":")
    try:
        if session_id.startswith(("qq:u:", "qq:temp:")) and len(parts) >= 3:
            return int(parts[2])
        if session_id.startswith("qq:g:") and len(parts) >= 5:
            return int(parts[4])
    except ValueError:
        return None
    return None


def _role(ctx: ToolContext) -> str:
    channel = getattr(ctx, "qq", None)
    contacts = getattr(channel, "contacts", None) if channel is not None else None
    if contacts is None:
        return "owner"  # 测试/CLI 场景
    user_id = _session_user_id(ctx.session_id)
    if user_id is None:
        return "friend"
    return contacts.role_of(user_id)


def _require(ctx: ToolContext, roles: set[str]) -> None:
    role = _role(ctx)
    if role in roles:
        return
    if roles == {"owner"}:
        raise ToolError("这个能力只有老爸能用。")
    raise ToolError("只有老爸或家人能让我这样做。")


async def _notify_owner(ctx: ToolContext, summary: str) -> None:
    """家人用手机代发消息后，给老爸留个通知（尽力而为）。"""
    if not getattr(ctx.settings, "phone_notify_owner", True):
        return
    if _role(ctx) != "family":
        return
    channel = getattr(ctx, "qq", None)
    contacts = getattr(channel, "contacts", None) if channel is not None else None
    sender = channel.send_text_to if channel is not None else None
    if contacts is None or sender is None or not contacts.owner:
        return
    try:
        await sender(contacts.owner, f"【手机代发】{summary}")
    except Exception:
        log.debug("代发通知失败", summary=summary)


def _client(ctx: ToolContext) -> PhoneClient:
    settings = ctx.settings
    if not getattr(settings, "phone_enabled", False):
        raise ToolError("手机助手没开（DSD_PHONE_ENABLED=false），先让老爸启动手机代理")
    return PhoneClient(
        getattr(settings, "phone_agent_url", "http://127.0.0.1:8765"),
        timeout=float(getattr(settings, "phone_timeout", 20.0)),
    )


async def _call(ctx: ToolContext, method: str, **kwargs) -> dict:
    client = _client(ctx)
    try:
        return await getattr(client, method)(**kwargs)
    except PhoneError as exc:
        raise ToolError(str(exc)) from exc


_COMMON_APPS = {
    "com.android.settings": "设置",
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.ss.android.ugc.aweme": "抖音",
    "com.eg.android.AlipayGphone": "支付宝（付款红线，不碰）",
    "com.taobao.taobao": "淘宝",
    "com.taobao.idlefish": "闲鱼",
    "com.sankuai.meituan": "美团",
    "me.ele": "饿了么",
    "com.jingdong.app.mall": "京东",
    "com.xunmeng.pinduoduo": "拼多多",
    "com.xingin.xhs": "小红书",
    "com.sina.weibo": "微博",
    "com.netease.cloudmusic": "网易云音乐",
    "com.autonavi.minimap": "高德地图",
    "com.tencent.map": "腾讯地图",
    "com.tencent.weread": "微信读书",
    "com.zhihu.android": "知乎",
    "com.google.android.youtube": "YouTube",
}


def _blocked_packages(settings) -> list[str]:
    return [
        item.strip().lower()
        for item in str(settings.phone_blocked_packages or "").split(",")
        if item.strip()
    ]


def _blocked_texts(settings) -> list[str]:
    return [
        item.strip() for item in str(settings.phone_blocked_texts or "").split(",") if item.strip()
    ]


def _check_package(ctx: ToolContext, package: str) -> None:
    lowered = package.lower()
    for keyword in _blocked_packages(ctx.settings):
        if keyword in lowered:
            raise ToolError(f"{package} 属于支付/银行/钱包类应用，我不能碰（红线）。")


def _check_text(ctx: ToolContext, text: str) -> None:
    for keyword in _blocked_texts(ctx.settings):
        if keyword and keyword in text:
            raise ToolError(f"「{text}」涉及支付/资金操作，我不能做（红线）。")


def _format_ui(data: dict) -> str:
    app = data.get("current_app") or "未知"
    texts = data.get("texts") or []
    lines = [f"当前 App：{app}"]
    if texts:
        lines.append("屏幕上的文字：")
        lines.extend(f"- {item}" for item in texts[:60])
    else:
        lines.append("屏幕上没读到文字（可能是图片/游戏/锁屏）。")
    return "\n".join(lines)


async def phone_status(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    data = await _call(ctx, "health")
    battery = data.get("battery")
    app = data.get("current_app") or "未知"
    stopped = "（已紧急停止）" if data.get("stopped") else ""
    return ToolResult.success(
        f"手机在线{stopped}：电量 {battery if battery is not None else '未知'}%，当前 App：{app}。"
    )


async def phone_screen_text(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    data = await _call(ctx, "ui")
    return ToolResult.success(_format_ui(data), current_app=data.get("current_app"))


async def phone_notifications(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    data = await _call(ctx, "notifications")
    texts = data.get("texts") or []
    if not texts:
        return ToolResult.success("通知栏是空的。")
    return ToolResult.success("通知栏上有：\n" + "\n".join(f"- {item}" for item in texts[:30]))


async def phone_screenshot(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    png = await _call(ctx, "screenshot")  # type: ignore[assignment]
    if not isinstance(png, (bytes, bytearray)):
        raise ToolError("截图返回格式不对")
    store = ScreenshotStore(getattr(ctx.settings, "data_dir", "data") / "phone_screenshots")
    path = store.save(bytes(png))
    return ToolResult.success(
        f"截好图了（{len(png) // 1024} KB），存在 {path}，老爸可以打开看。",
        path=str(path),
    )


async def phone_list_apps(ctx: ToolContext, keyword: str = "") -> ToolResult:
    _require(ctx, {"owner"})
    data = await _client(ctx).apps(all=bool(keyword))
    packages = [str(item) for item in data.get("packages") or []]
    keyword = (keyword or "").strip()
    if keyword:
        lowered = keyword.lower()
        packages = [
            item
            for item in packages
            if lowered in item.lower() or lowered in (_COMMON_APPS.get(item, "") or "").lower()
        ]
    if not packages:
        if keyword:
            return ToolResult.success(
                f"没找到名字里带「{keyword}」的应用。"
                "可以不带关键词列出全部，或者用包名（比如淘宝是 com.taobao.taobao）。"
            )
        return ToolResult.success("没找到应用。")
    lines = []
    for package in packages[:60]:
        label = _COMMON_APPS.get(package)
        lines.append(f"- {package}" + (f"（{label}）" if label else ""))
    return ToolResult.success("装着的应用：\n" + "\n".join(lines))


async def phone_open_app(ctx: ToolContext, package: str) -> ToolResult:
    _require(ctx, {"owner"})
    _check_package(ctx, package)
    await _client(ctx).open_app(package)
    label = _COMMON_APPS.get(package, package)
    return ToolResult.success(f"打开了{label}。")


async def phone_tap_text(ctx: ToolContext, text: str, timeout: float = 5.0) -> ToolResult:
    _require(ctx, {"owner"})
    _check_text(ctx, text)
    result = await _call(ctx, "tap_text", text=text, timeout=timeout)
    if not result.get("changed", True):
        return ToolResult.failure(
            f"屏幕上找到了「{text}」，但点完页面没变化——它可能只是标题/文字，不是按钮。"
            "可以换一个更具体的入口，或者先 phone_screen_text 看看。"
        )
    return ToolResult.success(f"点了屏幕上写着「{text}」的地方，页面变了。")


async def phone_type_text(ctx: ToolContext, text: str) -> ToolResult:
    _require(ctx, {"owner"})
    _check_text(ctx, text)
    await _client(ctx).type_text(text)
    return ToolResult.success(f"在输入框里打了「{text}」。")


async def phone_back(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    await _client(ctx).back()
    return ToolResult.success("按了返回。")


async def phone_look(ctx: ToolContext, question: str = "") -> ToolResult:
    _require(ctx, {"owner"})
    vision = build_vision(ctx.settings)
    if vision is None:
        raise ToolError("视觉模型没开（检查 DSD_VISION_ENABLED 和 Key）")
    png = await _call(ctx, "screenshot")
    if not isinstance(png, (bytes, bytearray)):
        raise ToolError("截图返回格式不对")
    try:
        description = await vision.describe(bytes(png), str(question or ""))
    except VisionError as exc:
        raise ToolError(f"眼睛没睁开：{exc}") from exc
    return ToolResult.success(f"我看到了：{description}")


async def _send_via_vision(
    ctx: ToolContext, app: str, label: str, contact: str, text: str
) -> ToolResult:
    _require(ctx, {"owner", "family"})
    _check_text(ctx, text)
    contact = str(contact or "").strip()
    if not contact:
        raise ToolError("要发给谁？给我个联系人名字")
    vision = build_vision(ctx.settings)
    if vision is None:
        raise ToolError("视觉模型没开，没法看屏幕操作")
    role = _role(ctx)
    if role == "family":
        channel = getattr(ctx, "qq", None)
        contacts = getattr(channel, "contacts", None) if channel is not None else None
        if contacts is not None and not contacts.is_family_recipient(contact):
            raise ToolError(
                f"老爸还没有授权给「{contact}」发消息。"
                f"先让老爸说一句「授权家人给 {contact} 发消息」。"
            )
    actual_text = text
    if role == "family" and "（鲸鲸代家人转达）" not in text:
        # 透明化：注明是代家人转达，不冒充老爸本人（避免重复粘贴时署名叠加）
        actual_text = f"{text}（鲸鲸代家人转达）"
    package = {"wechat": "com.tencent.mm", "qq": "com.tencent.mobileqq"}.get(app)
    if not package:
        raise ToolError("只支持微信/QQ")
    client = _client(ctx)
    # 先唤醒屏幕（锁屏下什么都做不了），再打开目标 App
    try:
        wake = await client.wake()
    except PhoneError as exc:
        raise ToolError(f"唤醒手机失败：{exc}") from exc
    if not wake.get("awake"):
        raise ToolError("手机屏幕没亮，没法看屏幕操作；让老爸检查一下是不是锁屏了")
    try:
        await client.open_app(package)
        await asyncio.sleep(2.5)
        health = await client.health()
    except PhoneError as exc:
        raise ToolError(f"打开{label}失败：{exc}") from exc
    if health.get("current_app") != package:
        raise ToolError(f"没能让{label}停在前台（当前是 {health.get('current_app')}），先不操作")
    flow_hint = ""
    if app == "wechat":
        flow_hint = (
            "微信操作顺序：底部「微信」tab → 右上角放大镜（搜索）→ 搜索框粘贴联系人名字 → "
            "点搜索结果里名字是「"
            + contact
            + "」的那一行 → 底部输入框粘贴消息 → 右下角绿色「发送」→ "
            "最后截图确认消息气泡出现。"
        )
    task = (
        f"你现在在{label}的首页（应用已经打开）。{flow_hint}"
        f"给联系人「{contact}」发送这条消息：「{actual_text}」，发送后确认消息已经出现在聊天记录里。"
        "这是家人委托的透明转达（消息末尾已注明），不是冒充老爸。不要发给其他人。"
    )
    try:
        summary = await run_phone_task(client, vision, task, max_steps=24)
    except PhoneTaskError as exc:
        # 视觉循环有时会误判"没发出去"。用具体问题二次确认，避免误报后重复发送。
        try:
            png = await client.screenshot()
            answer, _sent, _orig = await vision.ask(
                png,
                "看这张微信聊天截图：是否出现了一条内容包含「" + actual_text + "」的消息气泡？"
                "只回答一个字：是 或 否。",
            )
        except Exception:
            answer = ""
        if "是" not in answer[:8]:
            raise ToolError(
                f"手机代发没完成：{exc}。下次重试前先确认是不是已经发出去了，不要重复粘贴。"
            ) from exc
        summary = "二次确认：消息已在聊天记录里出现"
    await _notify_owner(ctx, f"{label}给「{contact}」发了：{actual_text}")
    return ToolResult.success(
        f"用{label}给「{contact}」发出去了：{actual_text}（{summary}）",
        app=app,
        contact=contact,
    )


async def phone_send_wechat(ctx: ToolContext, contact: str, text: str) -> ToolResult:
    return await _send_via_vision(ctx, "wechat", "微信", contact, text)


async def phone_send_qq(ctx: ToolContext, contact: str, text: str) -> ToolResult:
    return await _send_via_vision(ctx, "qq", "QQ", contact, text)


_PAYMENT_STOP_HINT = (
    "可以正常填写收货信息、选择地址、点「提交订单」进入支付页；"
    "但到了真正要付钱的页面（出现「立即支付 / 确认支付 / 输入支付密码 / 指纹支付 / 免密支付」），"
    "立刻停下来，不要输入密码、不要点支付，把当前页面告诉我。"
)


async def phone_do(ctx: ToolContext, task: str) -> ToolResult:
    _require(ctx, {"owner"})
    _check_text(ctx, task)
    task = f"{task}。{_PAYMENT_STOP_HINT}"
    vision = build_vision(ctx.settings)
    if vision is None:
        raise ToolError("视觉模型没开，没法看屏幕操作")
    client = _client(ctx)
    try:
        wake = await client.wake()
    except PhoneError as exc:
        raise ToolError(f"唤醒手机失败：{exc}") from exc
    if not wake.get("awake"):
        raise ToolError("手机屏幕没亮，没法看屏幕操作；让老爸检查一下是不是锁屏了")
    try:
        summary = await run_phone_task(client, vision, task, max_steps=16)
    except PhoneTaskError as exc:
        raise ToolError(f"任务没完成：{exc}") from exc
    return ToolResult.success(f"做完了：{summary}")


async def phone_prepare_order(ctx: ToolContext, what: str, app: str = "taobao") -> ToolResult:
    """帮她准备下单：操作到确认订单页就停，绝不付款（方案 A）。"""
    _require(ctx, {"owner"})
    _check_text(ctx, what)
    vision = build_vision(ctx.settings)
    if vision is None:
        raise ToolError("视觉模型没开，没法看屏幕操作")
    packages = {
        "taobao": ("com.taobao.taobao", "淘宝"),
        "meituan": ("com.sankuai.meituan", "美团"),
        "eleme": ("me.ele", "饿了么"),
        "ele": ("me.ele", "饿了么"),
    }
    package, label = packages.get(str(app or "").lower(), packages["taobao"])
    client = _client(ctx)
    try:
        wake = await client.wake()
        if not wake.get("awake"):
            raise ToolError("手机屏幕没亮，没法下单")
        health: dict = {}
        for _attempt in range(3):
            await client.open_app(package)
            await asyncio.sleep(3.0)
            health = await client.health()
            if health.get("current_app") == package:
                break
    except PhoneError as exc:
        raise ToolError(f"打开{label}失败：{exc}") from exc
    if health.get("current_app") != package:
        raise ToolError(f"没能让{label}停在前台（当前是 {health.get('current_app')}）")

    # 从长期记忆里翻老爸的收货信息（仅本单使用）
    shipping = ""
    channel = getattr(ctx, "qq", None)
    memory = getattr(getattr(channel, "agent", None), "memory", None)
    if memory is not None:
        try:
            recalled = await memory.recall(ctx.session_id, "收货地址 收货人 电话", limit=3)
            shipping = "；".join(
                str(getattr(item, "content", ""))
                for item in recalled
                if getattr(item, "content", "")
            )
        except Exception:
            pass
    shipping_hint = (
        f"老爸的收货信息（从记忆里找到的，仅用于这一单）：{shipping[:300]}。"
        if shipping
        else "如果页面需要填收货信息，先停下来问老爸。"
    )
    task = (
        f"帮老爸在{label}下单：{what}。{shipping_hint}"
        "流程：搜索/挑选商品→选规格→「立即购买」→确认订单页（填写/选择收货人、电话、地址）→"
        "点「提交订单」进入支付页→**到支付页就停**。"
        "**绝对不要**输入支付密码、不要点「立即支付 / 确认支付 / 指纹支付 / 免密支付」；"
        "到支付页后告诉我商品名、金额、当前页面。"
    )
    try:
        summary = await run_phone_task(client, vision, task, max_steps=28)
    except PhoneTaskError as exc:
        raise ToolError(f"准备下单没完成：{exc}") from exc
    except Exception as exc:  # 便于定位视觉/页面层异常
        import traceback

        log.error("prepare_order_crash", error=str(exc), trace=traceback.format_exc()[-1500:])
        raise ToolError(f"准备下单出错：{type(exc).__name__}: {exc}") from exc

    # 留一张截图给老爸确认，再用视觉描述当前页面
    path = ""
    try:
        png = await client.screenshot()
        store = ScreenshotStore(getattr(ctx.settings, "data_dir", "data") / "phone_screenshots")
        path = str(store.save(png))
        page = await vision.describe(
            png,
            "这是下单流程的页面。请回答：现在停在哪个页面？是商品页、确认订单页还是支付页？"
            "商品名和金额是多少？只描述页面，不要操作。",
        )
    except Exception:
        page = ""
    channel = getattr(ctx, "qq", None)
    memory = getattr(getattr(channel, "agent", None), "memory", None)
    if memory is not None:
        try:
            from ..memory.base import MemoryItem

            await memory.add_memories(
                ctx.session_id,
                [
                    MemoryItem(
                        content=f"我帮老爸准备了订单：{what}（{summary[:120]}），停在付款前，等他付",
                        kind="event",
                        keywords="下单 订单 付款前",
                        importance=0.5,
                        source="prepare_order",
                    )
                ],
            )
        except Exception:
            pass
    return ToolResult.success(
        f"准备好了，停在付款前：{summary}\n页面情况：{page}\n截图：{path}\n"
        "老爸你自己点付款就行，我不会碰支付。",
        path=path,
    )


async def phone_taobao_browse(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    vision = build_vision(ctx.settings)
    if vision is None:
        raise ToolError("视觉模型没开，没法看屏幕逛淘宝")
    client = _client(ctx)
    try:
        wake = await client.wake()
    except PhoneError as exc:
        raise ToolError(f"唤醒手机失败：{exc}") from exc
    if not wake.get("awake"):
        raise ToolError("手机屏幕没亮，逛不了淘宝")
    try:
        health: dict = {}
        for _attempt in range(3):
            await client.open_app("com.taobao.taobao")
            await asyncio.sleep(3.0)
            health = await client.health()
            if health.get("current_app") == "com.taobao.taobao":
                break
    except PhoneError as exc:
        raise ToolError(f"打开淘宝失败：{exc}") from exc
    if health.get("current_app") != "com.taobao.taobao":
        raise ToolError(f"没能让淘宝停在前台（当前是 {health.get('current_app')}）")
    try:
        summary = await run_taobao_browse(client, vision)
    except PhoneTaskError as exc:
        raise ToolError(f"淘宝没逛成：{exc}") from exc
    channel = getattr(ctx, "qq", None)
    memory = getattr(getattr(channel, "agent", None), "memory", None)
    if memory is not None:
        try:
            from ..memory.base import MemoryItem

            await memory.add_memories(
                ctx.session_id,
                [
                    MemoryItem(
                        content=f"我在淘宝把「{summary[:160]}」加进了购物车",
                        kind="event",
                        keywords="淘宝 购物车 喜欢",
                        importance=0.5,
                        source="taobao",
                    )
                ],
            )
        except Exception:
            pass
    return ToolResult.success(f"逛完淘宝啦：{summary}（已经加购物车，没付款）")


async def phone_stop(ctx: ToolContext) -> ToolResult:
    _require(ctx, {"owner"})
    await _call(ctx, "stop")
    return ToolResult.success("手机代理已紧急停止，之后所有动作都不会执行，直到老爸说恢复。")


def build_phone_tools() -> list[Tool]:
    return [
        Tool(
            name="phone_status",
            description="看看老爸手机在不在线、电量多少、当前开着什么 App。",
            parameters={"type": "object", "properties": {}},
            handler=phone_status,
        ),
        Tool(
            name="phone_screen_text",
            description=(
                "读老爸手机当前屏幕上显示的文字（相当于你凑过去看一眼屏幕）。只读，不会碰手机。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=phone_screen_text,
        ),
        Tool(
            name="phone_notifications",
            description="读老爸手机通知栏里有什么（谁发了消息、有什么提醒）。只读。",
            parameters={"type": "object", "properties": {}},
            handler=phone_notifications,
        ),
        Tool(
            name="phone_screenshot",
            description="给老爸的手机屏幕截一张图存下来（只读，存到本地给老爸看）。",
            parameters={"type": "object", "properties": {}},
            handler=phone_screenshot,
        ),
        Tool(
            name="phone_list_apps",
            description="列出老爸手机里装的应用（可以给关键词过滤），拿 package 名用。",
            parameters={
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "过滤关键词，可留空"}},
            },
            handler=phone_list_apps,
        ),
        Tool(
            name="phone_open_app",
            description=(
                "打开老爸手机上的某个 App（用 package 名，比如 com.tencent.mm 是微信）。"
                "支付/银行类会被拒绝。"
            ),
            parameters={
                "type": "object",
                "properties": {"package": {"type": "string", "description": "应用包名"}},
                "required": ["package"],
            },
            handler=phone_open_app,
            is_action=True,
        ),
        Tool(
            name="phone_tap_text",
            description="按屏幕上的文字点击（比如点「电池」「搜索」）。涉及支付/转账的文字会被拒绝。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要点击的文字"},
                },
                "required": ["text"],
            },
            handler=phone_tap_text,
            is_action=True,
        ),
        Tool(
            name="phone_type_text",
            description="在当前输入框里打字（会先切到她的输入法再输入）。涉及支付口令会被拒绝。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要输入的内容"}},
                "required": ["text"],
            },
            handler=phone_type_text,
            is_action=True,
        ),
        Tool(
            name="phone_back",
            description="按一下手机的返回键。",
            parameters={"type": "object", "properties": {}},
            handler=phone_back,
            is_action=True,
        ),
        Tool(
            name="phone_look",
            description=(
                "看一眼老爸手机现在的屏幕（截图 + 多模态视觉），用文字描述你看到了什么。"
                "比 phone_screen_text 更强：能看懂图片、图标、布局。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "你想看图里的什么，可留空"}
                },
            },
            handler=phone_look,
        ),
        Tool(
            name="phone_do",
            description=(
                "让鲸鲸看屏幕一步一步操作手机完成一个任务（视觉驱动），"
                "比如「打开设置把蓝牙关掉」「在微信里给文件传输助手发一句话」。"
                "只有老爸能用；涉及支付/银行会被拒绝。"
            ),
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string", "description": "用一句中文描述要做什么"}},
                "required": ["task"],
            },
            handler=phone_do,
            is_action=True,
        ),
        Tool(
            name="phone_send_wechat",
            description=(
                "用老爸手机上的微信给某个联系人发消息（只有老爸或家人能让我这么做）。"
                "会打开微信→搜索联系人→输入→发送，并验证发出去没有。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "微信联系人名字（要能搜到）"},
                    "text": {"type": "string", "description": "要发的内容"},
                },
                "required": ["contact", "text"],
            },
            handler=phone_send_wechat,
            is_action=True,
        ),
        Tool(
            name="phone_send_qq",
            description=(
                "用老爸手机上的 QQ 给某个联系人发消息（只有老爸或家人能让我这么做）。"
                "会打开 QQ→搜索联系人→输入→发送，并验证发出去没有。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "QQ 联系人名字"},
                    "text": {"type": "string", "description": "要发的内容"},
                },
                "required": ["contact", "text"],
            },
            handler=phone_send_qq,
            is_action=True,
        ),
        Tool(
            name="phone_prepare_order",
            description=(
                "帮老爸准备下单（淘宝/美团/饿了么）：搜索或挑选商品，操作到确认订单页就停下，"
                "绝不点支付/付款/提交订单，等老爸自己付款。老爸让你点外卖、买东西时用这个。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "要买什么，比如「一杯热美式」"},
                    "app": {
                        "type": "string",
                        "enum": ["taobao", "meituan", "eleme"],
                        "description": "用哪个 App，默认淘宝",
                    },
                },
                "required": ["what"],
            },
            handler=phone_prepare_order,
            is_action=True,
        ),
        Tool(
            name="phone_taobao_browse",
            description=(
                "用老爸的手机刷淘宝，逛一逛、挑喜欢的东西加入购物车（只加购，绝不付款/下单）。"
                "老大或老爸让你逛、或者你想逛街时用。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=phone_taobao_browse,
            is_action=True,
        ),
        Tool(
            name="phone_stop",
            description="紧急停止手机代理：如果手机操作出问题，立刻停掉所有动作。",
            parameters={"type": "object", "properties": {}},
            handler=phone_stop,
            is_action=True,
        ),
    ]
