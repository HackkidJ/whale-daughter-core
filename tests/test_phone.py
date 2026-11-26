"""手机代理：服务端、客户端、工具（只读）。"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from dsdaughter.config import Settings
from dsdaughter.phone.client import PhoneClient, PhoneError
from dsdaughter.phone.service import create_phone_app, parse_notifications, texts_from_xml
from dsdaughter.tools import ToolContext, ToolError
from dsdaughter.tools import phone as phone_tools

BASE = "http://127.0.0.1:8765"


class FakeSelector:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.clicked = False

    def wait(self, timeout: float = 0) -> bool:
        return self.exists

    def click(self) -> None:
        self.clicked = True


class FakeDevice:
    def __init__(self) -> None:
        self.clicks: list[str] = []
        self.presses: list[str] = []
        self.typed: list[str] = []
        self.started = ""
        self._selector = FakeSelector()

    def info(self) -> dict:
        return {"displayWidth": 1080, "displayHeight": 2400, "productName": "OPPO"}

    def dump_hierarchy(self) -> str:
        return '<node text="微信" /><node text="老爸：在吗" /><node content-desc="发送" />'

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (8, 8), "black")

    def open_notification(self) -> None:
        return None

    def press(self, key: str) -> None:
        self.presses.append(key)

    def app_start(self, package: str) -> None:
        self.started = package

    def app_current(self) -> dict:
        return {"package": "com.tencent.mm"}

    def shell(self, command: str) -> str:
        return "level: 88"

    def __call__(self, *args, **kwargs) -> FakeSelector:
        return self._selector

    def xpath(self, query: str) -> FakeSelector:
        return self._selector

    def set_fastinput_ime(self, flag: bool) -> None:
        return None

    def send_keys(self, text: str, clear: bool = True) -> None:
        self.typed.append(text)


def make_client(factory=None) -> TestClient:
    device = factory or FakeDevice()
    return TestClient(create_phone_app(lambda: device))


def test_parse_notifications():
    sample = """
      android.title=String (老爸)
      android.text=String (在吗)
      android.title=String (老爸)
      android.text=String (吃饭没)
    """
    assert parse_notifications(sample) == ["老爸", "在吗", "吃饭没"]


def test_texts_from_xml_dedupes():
    assert texts_from_xml('<node text="a" /><node content-desc="a" /><node text="b" />') == [
        "a",
        "b",
    ]


def test_service_health_ui_screenshot():
    client = make_client()
    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["battery"] == 88
    assert health["current_app"] == "com.tencent.mm"

    ui = client.get("/ui").json()
    assert "老爸：在吗" in ui["summary"]

    shot = client.get("/screenshot")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(shot.content)).size == (8, 8)

    notes = client.get("/notifications").json()
    assert notes["ok"] is True


def test_service_actions_and_stop():
    client = make_client()
    assert client.post("/open_app", json={"package": "com.tencent.mm"}).json()["ok"]
    tap = client.post("/tap_text", json={"text": "微信"}).json()
    assert tap["ok"] is True
    assert "changed" in tap
    assert client.post("/type_text", json={"text": "你好"}).json()["ok"]
    assert client.post("/back").json()["ok"]

    client.post("/stop")
    blocked = client.post("/open_app", json={"package": "com.x"})
    assert blocked.status_code == 409
    client.post("/resume")
    assert client.post("/open_app", json={"package": "com.x"}).status_code == 200


def test_service_device_error():
    factory = lambda: (_ for _ in ()).throw(RuntimeError("no device"))  # noqa: E731
    client = TestClient(create_phone_app(factory))
    response = client.get("/health")
    assert response.status_code == 503


# --------------------------------------------------------------------- 客户端
@respx.mock
async def test_client_roundtrip():
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "battery": 90})
    )
    respx.get(f"{BASE}/ui").mock(
        return_value=httpx.Response(200, json={"texts": ["你好"], "summary": "你好"})
    )
    respx.get(f"{BASE}/screenshot").mock(return_value=httpx.Response(200, content=b"png"))
    client = PhoneClient(BASE)
    assert (await client.health())["battery"] == 90
    assert (await client.ui())["texts"] == ["你好"]
    assert await client.screenshot() == b"png"


@respx.mock
async def test_client_error():
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(503, json={"detail": "手机没连上"})
    )
    with pytest.raises(PhoneError):
        await PhoneClient(BASE).health()


# --------------------------------------------------------------------- 工具
def make_settings(tmp_path: Path, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        phone_enabled=enabled,
        phone_agent_url=BASE,
        data_dir=tmp_path,
        memory_extract=False,
        vision_enabled=True,
        deepseek_api_key="sk-test",
    )


def make_ctx(tmp_path: Path, enabled: bool = True) -> ToolContext:
    return ToolContext(settings=make_settings(tmp_path, enabled), session_id="qq:u:1", qq=object())


@respx.mock
async def test_tools_readonly(tmp_path):
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "battery": 77, "current_app": "com.tencent.mm"}
        )
    )
    respx.get(f"{BASE}/ui").mock(
        return_value=httpx.Response(
            200, json={"current_app": "com.tencent.mm", "texts": ["老爸：在吗"], "summary": ""}
        )
    )
    respx.get(f"{BASE}/notifications").mock(
        return_value=httpx.Response(200, json={"texts": ["微信：老爸"], "summary": ""})
    )
    respx.get(f"{BASE}/screenshot").mock(return_value=httpx.Response(200, content=b"pngdata"))
    ctx = make_ctx(tmp_path)

    status = await phone_tools.phone_status(ctx)
    assert "77" in status.content
    assert "com.tencent.mm" in status.content

    ui = await phone_tools.phone_screen_text(ctx)
    assert "老爸：在吗" in ui.content

    notes = await phone_tools.phone_notifications(ctx)
    assert "微信：老爸" in notes.content

    shot = await phone_tools.phone_screenshot(ctx)
    assert shot.ok
    assert Path(shot.data["path"]).exists()


async def test_tools_disabled(tmp_path):
    ctx = make_ctx(tmp_path, enabled=False)
    with pytest.raises(ToolError):
        await phone_tools.phone_status(ctx)


# --------------------------------------------------------------------- 动作类
def test_service_apps_list():
    class AppDevice(FakeDevice):
        def shell(self, command: str) -> str:
            return "package:com.tencent.mm\npackage:com.android.settings\n"

    client = TestClient(create_phone_app(lambda: AppDevice()))
    data = client.get("/apps").json()
    assert "com.tencent.mm" in data["packages"]


@respx.mock
async def test_tools_actions(tmp_path):
    respx.get(f"{BASE}/apps").mock(
        return_value=httpx.Response(
            200,
            json={
                "packages": ["com.tencent.mm", "com.android.settings", "com.taobao.taobao"],
                "total": 3,
            },
        )
    )
    respx.post(f"{BASE}/open_app").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.post(f"{BASE}/tap_text").mock(
        return_value=httpx.Response(200, json={"ok": True, "changed": True})
    )
    respx.post(f"{BASE}/type_text").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.post(f"{BASE}/back").mock(return_value=httpx.Response(200, json={"ok": True}))
    ctx = make_ctx(tmp_path)

    apps = await phone_tools.phone_list_apps(ctx, keyword="淘宝")
    assert "com.taobao.taobao" in apps.content
    assert "com.tencent.mm" not in apps.content

    opened = await phone_tools.phone_open_app(ctx, package="com.android.settings")
    assert opened.ok
    tapped = await phone_tools.phone_tap_text(ctx, text="电池")
    assert tapped.ok
    typed = await phone_tools.phone_type_text(ctx, text="你好")
    assert typed.ok
    assert (await phone_tools.phone_back(ctx)).ok


async def test_tools_payment_red_lines(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        await phone_tools.phone_open_app(ctx, package="com.eg.android.AlipayGphone")
    with pytest.raises(ToolError):
        await phone_tools.phone_tap_text(ctx, text="确认支付")
    with pytest.raises(ToolError):
        await phone_tools.phone_type_text(ctx, text="转账 500")


# --------------------------------------------------------------------- 视觉 & 角色 & 代发
class FakeContacts:
    owner = 1001

    def role_of(self, user_id: int) -> str:
        return {1001: "owner", 1002: "family"}.get(user_id, "friend")

    def is_family_recipient(self, name: str) -> bool:
        return True


class FakeChannel:
    def __init__(self) -> None:
        self.contacts = FakeContacts()
        self.notices: list[tuple[int, str]] = []

    async def send_text_to(self, user_id: int, text: str) -> int:
        self.notices.append((user_id, text))
        return 1


def ctx_with(session_id: str, tmp_path) -> ToolContext:
    s = make_settings(tmp_path)
    return ToolContext(settings=s, session_id=session_id, qq=FakeChannel())


@respx.mock
async def test_phone_look_uses_vision(tmp_path):
    png_buffer = io.BytesIO()
    Image.new("RGB", (64, 128), "white").save(png_buffer, format="PNG")
    respx.get(f"{BASE}/screenshot").mock(
        return_value=httpx.Response(200, content=png_buffer.getvalue())
    )
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "看到的是微信聊天列表。"}}]}
        )
    )
    result = await phone_tools.phone_look(ctx_with("qq:u:1001", tmp_path))
    assert "微信聊天列表" in result.content


async def test_phone_read_requires_owner(tmp_path):
    with pytest.raises(ToolError):
        await phone_tools.phone_screen_text(ctx_with("qq:u:1002", tmp_path))
    with pytest.raises(ToolError):
        await phone_tools.phone_open_app(ctx_with("qq:u:1002", tmp_path), package="com.tencent.mm")


@respx.mock
async def test_family_can_send_qq(tmp_path, monkeypatch):
    from dsdaughter.tools import phone as phone_module

    respx.get(f"{BASE}/wake").mock(
        return_value=httpx.Response(200, json={"ok": True, "awake": True})
    )
    respx.post(f"{BASE}/open_app").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "current_app": "com.tencent.mobileqq"})
    )

    tasks: list[str] = []

    async def fake_task(client, vision, task, max_steps=14):
        tasks.append(task)
        return "点了搜索、发了消息"

    monkeypatch.setattr(phone_module, "run_phone_task", fake_task)
    ctx = ctx_with("qq:u:1002", tmp_path)
    result = await phone_tools.phone_send_qq(ctx, contact="我的电脑", text="测试一下")
    assert result.ok
    assert "我的电脑" in result.content
    assert "测试一下" in tasks[0]
    assert ctx.qq.notices  # 家人代发后给老爸留通知


async def test_friend_cannot_send(tmp_path):
    with pytest.raises(ToolError):
        await phone_tools.phone_send_qq(ctx_with("qq:u:999", tmp_path), contact="x", text="y")


# --------------------------------------------------------------------- 视觉操作循环
class FakePhoneScreens:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.pastes: list[str] = []
        self.screens = 0

    async def screenshot(self) -> bytes:
        self.screens += 1
        png_buffer = io.BytesIO()
        Image.new("RGB", (1080, 2400), "white").save(png_buffer, format="PNG")
        return png_buffer.getvalue()

    async def tap_xy(self, x: int, y: int) -> dict:
        self.taps.append((x, y))
        return {"ok": True}

    async def paste_text(self, text: str) -> dict:
        self.pastes.append(text)
        return {"ok": True}

    async def back(self) -> dict:
        return {"ok": True}


class FakeActVision:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.index = 0

    async def ask(self, image, prompt, *, max_tokens=None):
        reply = self.replies[min(self.index, len(self.replies) - 1)]
        self.index += 1
        return reply, (720, 1600), (1080, 2400)


async def test_vision_agent_steps():
    from dsdaughter.phone.vision_agent import run_phone_task

    client = FakePhoneScreens()
    vision = FakeActVision(
        [
            '{"action":"tap","x":360,"y":100,"reason":"点搜索"}',
            '{"action":"paste","text":"文件传输助手","reason":"输入联系人"}',
            '{"action":"done","summary":"发出去了"}',
        ]
    )
    summary = await run_phone_task(client, vision, "测试任务", max_steps=5)
    assert "发出去了" in summary
    assert client.taps == [(540, 150)]  # 720->1080, 1600->2400 缩放
    assert client.pastes == ["文件传输助手"]


async def test_vision_agent_failed():
    from dsdaughter.phone.vision_agent import PhoneTaskError, run_phone_task

    vision = FakeActVision(['{"action":"failed","reason":"屏幕上没有这个按钮"}'])
    with pytest.raises(PhoneTaskError):
        await run_phone_task(FakePhoneScreens(), vision, "测试任务", max_steps=3)


@respx.mock
async def test_prepare_order_stops_before_payment(tmp_path, monkeypatch):
    from dsdaughter.tools import phone as phone_module

    png_buffer = io.BytesIO()
    Image.new("RGB", (64, 128), "white").save(png_buffer, format="PNG")
    respx.get(f"{BASE}/wake").mock(
        return_value=httpx.Response(200, json={"ok": True, "awake": True})
    )
    respx.post(f"{BASE}/open_app").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "current_app": "com.taobao.taobao"})
    )
    respx.get(f"{BASE}/screenshot").mock(
        return_value=httpx.Response(200, content=png_buffer.getvalue())
    )
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "停在确认订单页，商品：热美式，金额 9.9 元"}}]
            },
        )
    )
    tasks: list[str] = []

    async def fake_task(client, vision, task, max_steps=28):
        tasks.append(task)
        return "已进入支付页，金额 9.9 元"

    monkeypatch.setattr(phone_module, "run_phone_task", fake_task)
    ctx = make_ctx(tmp_path)
    result = await phone_tools.phone_prepare_order(ctx, what="一杯热美式")
    assert result.ok
    assert "付款" in result.content
    assert tasks and "提交订单" in tasks[0] and "支付密码" in tasks[0]
    assert Path(result.data["path"]).exists()


class FakeBadJsonVision:
    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, image, prompt, *, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            return (
                '{"action":"tap","x":100,"y":200,"reason":"准备点击顶部搜索框，搜索热美式"',
                (720, 1600),
                (1080, 2400),
            )
        return '{"action":"done","summary":"已经停在确认订单页"}', (720, 1600), (1080, 2400)


async def test_vision_agent_retries_bad_json():
    from dsdaughter.phone.vision_agent import run_phone_task

    vision = FakeBadJsonVision()
    summary = await run_phone_task(FakePhoneScreens(), vision, "测试任务", max_steps=3)
    assert "确认订单页" in summary
    assert vision.calls == 2
