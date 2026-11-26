"""手机设备代理（独立服务）：通过 adb + uiautomator2 操作安卓手机。

设计成独立进程，bot 只通过 HTTP 调它：
- bot 不需要装 uiautomator2，也不需要跟手机在同一台机器
- 手机挂了/换设备不影响聊天
- 所有设备动作集中在一个地方，方便审计和"紧急停止"

启动（先在电脑上配好 adb 无线连接）：

    tools/platform-tools/adb pair 192.168.1.x:37xxx 123456
    tools/platform-tools/adb connect 192.168.1.x:37xxx
    DSD_PHONE_ADB_ADDRESS=192.168.1.x:37xxx \\
        uv run uvicorn dsdaughter.phone.service:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

import structlog
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

log = structlog.get_logger(__name__)

_TEXT_RE = re.compile(r'(?:text|content-desc)="([^"]{1,300})"')


class DeviceProtocol(Protocol):
    def info(self) -> dict[str, Any]: ...
    def dump_hierarchy(self) -> str: ...
    def screenshot(self) -> Any: ...
    def open_notification(self) -> None: ...
    def press(self, key: str) -> None: ...
    def app_start(self, package: str) -> None: ...
    def app_current(self) -> dict[str, Any]: ...
    def shell(self, command: str) -> Any: ...


def default_device() -> DeviceProtocol:
    import uiautomator2 as u2

    address = os.environ.get("DSD_PHONE_ADB_ADDRESS", "").strip()
    device = u2.connect(address) if address else u2.connect()
    device.settings["wait_timeout"] = 5.0
    return device


def texts_from_xml(xml: str, limit: int = 120) -> list[str]:
    seen: set[str] = set()
    texts: list[str] = []
    for match in _TEXT_RE.findall(xml or ""):
        value = match.strip()
        if value and value not in seen:
            seen.add(value)
            texts.append(value)
            if len(texts) >= limit:
                break
    return texts


def _shell_output(result: Any) -> str:
    """uiautomator2 3.x 的 shell() 返回 ShellResponse，取它的 .output。"""
    output = getattr(result, "output", None)
    if isinstance(output, str):
        return output
    return result if isinstance(result, str) else str(result)


def _device_info(device: DeviceProtocol) -> dict[str, Any]:
    """uiautomator2 3.x 的 info 是属性，2.x 是方法。"""
    info = device.info  # type: ignore[attr-defined]
    value = info() if callable(info) else info
    return value if isinstance(value, dict) else {}


def _battery(device: DeviceProtocol) -> int | None:
    try:
        result = device.shell("dumpsys battery")
        match = re.search(r"level:\s*(\d+)", _shell_output(result))
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _current_app(device: DeviceProtocol) -> str:
    try:
        info = device.app_current() or {}
        return str(info.get("package") or "")
    except Exception:
        return ""


_NOTIF_RE = re.compile(r"android\.(?:title|text)=String \((.*)\)")


def parse_notifications(output: str, limit: int = 30) -> list[str]:
    """从 dumpsys notification 里提取标题/正文，不打开通知栏。"""
    seen: set[str] = set()
    texts: list[str] = []
    for value in _NOTIF_RE.findall(output or ""):
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        texts.append(item)
        if len(texts) >= limit:
            break
    return texts


def _summary(texts: list[str]) -> str:
    return " | ".join(texts[:20])


class PhoneState:
    """进程内的共享状态：设备句柄 + 紧急停止开关。"""

    def __init__(self, factory: Callable[[], DeviceProtocol] | None = None) -> None:
        self._factory = factory or default_device
        self._device: DeviceProtocol | None = None
        self.stopped = False

    def device(self) -> DeviceProtocol:
        if self._device is None:
            self._device = self._factory()
        return self._device

    def ensure_actions_allowed(self) -> None:
        if self.stopped:
            raise HTTPException(status_code=409, detail="已紧急停止，先解除再操作")


class OpenAppRequest(BaseModel):
    package: str


class TapTextRequest(BaseModel):
    text: str
    timeout: float = 5.0


class TypeTextRequest(BaseModel):
    text: str


class TapXYRequest(BaseModel):
    x: int
    y: int


class PasteTextRequest(BaseModel):
    text: str


class SendMessageRequest(BaseModel):
    app: str  # wechat / qq
    contact: str
    text: str


def create_phone_app(factory: Callable[[], DeviceProtocol] | None = None) -> FastAPI:
    app = FastAPI(title="鲸鲸的手机代理", version="0.1.0")
    state = PhoneState(factory)

    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        try:
            device = state.device()
            info = await run(_device_info, device)
            return {
                "ok": True,
                "stopped": state.stopped,
                "battery": _battery(device),
                "current_app": _current_app(device),
                "device": {
                    k: info.get(k) for k in ("displayWidth", "displayHeight", "productName")
                },
            }
        except HTTPException:
            raise
        except Exception as exc:  # 设备没连上
            raise HTTPException(status_code=503, detail=f"手机没连上：{exc}") from exc

    @app.get("/ui")
    async def ui() -> dict[str, Any]:
        try:
            device = state.device()
            xml = await run(device.dump_hierarchy)
            texts = texts_from_xml(xml)
            return {
                "ok": True,
                "current_app": _current_app(device),
                "texts": texts,
                "summary": _summary(texts),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"读取屏幕失败：{exc}") from exc

    @app.get("/screenshot")
    async def screenshot() -> Response:
        try:
            device = state.device()
            image = await run(device.screenshot)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return Response(content=buffer.getvalue(), media_type="image/png")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"截图失败：{exc}") from exc

    @app.get("/notifications")
    async def notifications() -> dict[str, Any]:
        try:
            device = state.device()
            # 直接读通知服务，不打开下拉栏（open_notification 会留在用户屏幕上）
            output = await run(device.shell, "dumpsys notification --noredact")
            texts = parse_notifications(_shell_output(output))
            return {"ok": True, "texts": texts, "summary": _summary(texts)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"读通知失败：{exc}") from exc

    @app.get("/apps")
    async def apps(all: bool = False) -> dict[str, Any]:
        try:
            device = state.device()
            command = "pm list packages" if all else "pm list packages -3"
            output = await run(device.shell, command)
            packages = sorted(
                line.strip().removeprefix("package:")
                for line in _shell_output(output).splitlines()
                if line.strip().startswith("package:")
            )
            # 不截断：全部应用 400+ 个，截断会把排在后面的淘宝/闲鱼切掉
            return {"ok": True, "packages": packages, "total": len(packages)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"列应用失败：{exc}") from exc

    @app.post("/open_app")
    async def open_app(request: OpenAppRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await run(device.app_start, request.package)
            return {"ok": True, "package": request.package}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"打开 App 失败：{exc}") from exc

    @app.post("/tap_text")
    async def tap_text(request: TapTextRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            found, changed = await asyncio.to_thread(
                _click_text, device, request.text, request.timeout
            )
            if not found:
                raise HTTPException(status_code=404, detail=f"屏幕上没有「{request.text}」")
            return {"ok": True, "text": request.text, "changed": changed}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"点击失败：{exc}") from exc

    @app.post("/type_text")
    async def type_text(request: TypeTextRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await asyncio.to_thread(_type_text, device, request.text)
            return {"ok": True, "chars": len(request.text)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"输入失败：{exc}") from exc

    @app.get("/wake")
    async def wake() -> dict[str, Any]:
        try:
            device = state.device()
            awake = await asyncio.to_thread(_wake, device)
            return {"ok": True, "awake": awake}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"唤醒失败：{exc}") from exc

    @app.post("/clear_text")
    async def clear_text() -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await asyncio.to_thread(_clear_text, device)
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"清空输入框失败：{exc}") from exc

    @app.post("/paste_text")
    async def paste_text(request: PasteTextRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await asyncio.to_thread(_paste_text, device, request.text)
            return {"ok": True, "chars": len(request.text)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"粘贴失败：{exc}") from exc

    @app.post("/tap_xy")
    async def tap_xy(request: TapXYRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await asyncio.to_thread(_tap_xy, device, request.x, request.y)
            return {"ok": True, "x": request.x, "y": request.y}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"坐标点击失败：{exc}") from exc

    @app.post("/send_message")
    async def send_message(request: SendMessageRequest) -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            result = await asyncio.to_thread(
                _send_message_recipe, device, request.app, request.contact, request.text
            )
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"代发消息失败：{exc}") from exc

    @app.post("/back")
    async def back() -> dict[str, Any]:
        state.ensure_actions_allowed()
        try:
            device = state.device()
            await run(device.press, "back")
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"返回失败：{exc}") from exc

    @app.post("/stop")
    async def stop() -> dict[str, Any]:
        state.stopped = True
        log.warning("手机代理已紧急停止")
        return {"ok": True, "stopped": True}

    @app.post("/resume")
    async def resume() -> dict[str, Any]:
        state.stopped = False
        return {"ok": True, "stopped": False}

    return app


def _dump(device: DeviceProtocol) -> str:
    try:
        return str(device.dump_hierarchy())
    except Exception:
        return ""


def _screen_awake(device: DeviceProtocol) -> bool:
    try:
        output = _shell_output(device.shell("dumpsys power"))
        return "mWakefulness=Awake" in output
    except Exception:
        return False


def _keyguard_showing(device: DeviceProtocol) -> bool:
    try:
        output = _shell_output(device.shell("dumpsys window"))
        return "mDreamingLockscreen=true" in output or "KeyguardShowing" in output
    except Exception:
        return False


def _wake(device: DeviceProtocol) -> bool:
    """点亮屏幕；如果是向上滑就能解的锁屏顺带划一下（不会碰密码锁）。"""
    if not _screen_awake(device):
        device.shell("input keyevent 224")  # KEYCODE_WAKEUP
        time.sleep(1.0)
    if _keyguard_showing(device):
        try:
            device.shell("input swipe 540 2100 540 500 200")
            time.sleep(0.8)
        except Exception:
            pass
    return _screen_awake(device)


def _clear_text(device: DeviceProtocol) -> None:
    # 优先用无障碍清空标准输入框（QQ 这类可访问的）
    try:
        edit = device(className="android.widget.EditText")  # type: ignore[operator]
        if edit.exists:
            edit.set_text("")
            return
    except Exception:
        pass
    try:
        device.clear_text()  # type: ignore[attr-defined]
        return
    except Exception:
        pass
    device.shell("input keyevent 123")  # MOVE_END
    for _ in range(80):
        device.shell("input keyevent 67")  # DEL


def _paste_text(device: DeviceProtocol, text: str) -> None:
    """优先用无障碍 set_text 写输入框（中文可用）；微信这种屏蔽无障碍的再退化到剪贴板粘贴。

    uiautomator2 的 set_clipboard 依赖 ADBKeyboard 输入法；当前是搜狗/百度时剪贴板设不进去，
    所以临时切到 ADBKeyboard，粘贴完再切回原输入法。
    """
    # 优先写当前聚焦的输入框；再退到第一个 EditText（淘宝/QQ 这类可访问的）
    for selector in (
        lambda: device(focused=True),  # type: ignore[operator]
        lambda: device(className="android.widget.EditText"),  # type: ignore[operator]
    ):
        try:
            edit = selector()
            if edit.exists:
                edit.set_text(text)
                return
        except Exception:
            continue

    previous_ime = ""
    with contextlib.suppress(Exception):
        previous_ime = _shell_output(
            device.shell("settings get secure default_input_method")
        ).strip()
    # 粘贴前先清空，避免重试时文字叠两遍
    with contextlib.suppress(Exception):
        device.shell("input keyevent 123")  # MOVE_END
        for _ in range(60):
            device.shell("input keyevent 67")  # DEL
    try:
        device.set_fastinput_ime(True)  # type: ignore[attr-defined]
        time.sleep(0.4)
        device.set_clipboard(text)  # type: ignore[attr-defined]
        time.sleep(0.3)
        device.shell("input keyevent 279")  # KEYCODE_PASTE
    except Exception as exc:
        raise RuntimeError(f"粘贴失败：{exc}") from exc
    finally:
        if previous_ime:
            with contextlib.suppress(Exception):
                device.shell(f"ime set {previous_ime}")


def _tap_xy(device: DeviceProtocol, x: int, y: int) -> None:
    try:
        device.click(x, y)  # type: ignore[attr-defined]
    except Exception:
        device.shell(f"input tap {int(x)} {int(y)}")


def _click_text(device: DeviceProtocol, text: str, timeout: float) -> tuple[bool, bool]:
    """按文字点控件，并校验点击后屏幕有没有变化。

    返回 (找到没有, 页面变没变)。找不到时 (False, False)。
    很多文字是分组标题/搜索结果的纯文本，点了没反应——要如实报告。
    """
    before = _dump(device)
    found = False
    try:
        selector = device(text=text)  # type: ignore[operator]
        if selector.wait(timeout=timeout):
            found = True
            selector.click()
    except Exception:
        pass
    if not found:
        try:
            selector = device.xpath(f'//*[@text="{text}"]')  # type: ignore[attr-defined]
            if selector.wait(timeout=1.0):
                found = True
                selector.click()
        except Exception:
            pass
    if not found:
        try:
            selector = device.xpath(f'//*[contains(@text, "{text}")]')  # type: ignore[attr-defined]
            if selector.wait(timeout=1.0):
                found = True
                selector.click()
        except Exception:
            return False, False
    if not found:
        return False, False

    time.sleep(0.8)
    if _dump(device) != before:
        return True, True

    # 点的是不可点的文字：试试点它最近的可点父节点
    try:
        ancestor = device.xpath(  # type: ignore[attr-defined]
            f'//*[@text="{text}"]/ancestor::*[@clickable="true"][1]'
        )
        if ancestor.wait(timeout=1.0):
            ancestor.click()
            time.sleep(0.8)
            return True, _dump(device) != before
    except Exception:
        pass
    return True, False


_MESSAGING_APPS = {"wechat": "com.tencent.mm", "qq": "com.tencent.mobileqq"}


def _click_first(
    device: DeviceProtocol, selectors: list[Callable[[], Any]], timeout: float
) -> bool:
    for factory in selectors:
        try:
            selector = factory()
            if selector.wait(timeout=timeout):
                selector.click()
                return True
        except Exception:
            continue
    return False


def _type_into(device: DeviceProtocol, text: str) -> None:
    """优先用 set_text 写 EditText（支持中文），否则退回 send_keys。"""
    try:
        edit = device(className="android.widget.EditText")  # type: ignore[operator]
        if edit.exists:
            edit.set_text(text)
            return
    except Exception:
        pass
    device.set_fastinput_ime(True)  # type: ignore[attr-defined]
    device.send_keys(text, clear=True)  # type: ignore[attr-defined]
    device.set_fastinput_ime(False)  # type: ignore[attr-defined]


def _send_message_recipe(
    device: DeviceProtocol, app: str, contact: str, text: str
) -> dict[str, Any]:
    """打开微信/QQ，搜索联系人，发送消息，最后用屏幕内容验证。"""
    package = _MESSAGING_APPS.get(app.lower())
    if not package:
        raise RuntimeError("只支持 wechat / qq")
    if not contact.strip() or not text.strip():
        raise RuntimeError("联系人和内容不能为空")

    steps: list[str] = []
    device.app_start(package)
    time.sleep(2.5)
    steps.append("open_app")

    if not _click_first(
        device,
        [
            lambda: device(description="搜索"),  # type: ignore[operator]
            lambda: device(text="搜索"),  # type: ignore[operator]
            lambda: device(textContains="搜索"),  # type: ignore[operator]
        ],
        timeout=3.0,
    ):
        raise RuntimeError("没找到搜索入口（微信/QQ 首页）")
    steps.append("tap_search")
    time.sleep(1.0)

    _type_into(device, contact)
    steps.append("type_contact")
    time.sleep(2.0)

    try:
        result = device(textContains=contact)  # type: ignore[operator]
        if not result.exists:
            raise RuntimeError(f"没搜到联系人「{contact}」")
        result.click()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"点联系人失败：{exc}") from exc
    steps.append("tap_contact")
    time.sleep(2.0)

    _type_into(device, text)
    steps.append("type_text")
    time.sleep(1.0)

    if not _click_first(
        device,
        [
            lambda: device(text="发送"),  # type: ignore[operator]
            lambda: device(description="发送"),  # type: ignore[operator]
        ],
        timeout=3.0,
    ):
        raise RuntimeError("没找到发送按钮")
    steps.append("tap_send")
    time.sleep(1.5)

    dump = _dump(device)
    if text.strip()[:20] not in dump:
        raise RuntimeError("发送后屏幕上没看到消息，可能没发出去")
    return {"ok": True, "steps": steps, "app": app, "contact": contact}


def _type_text(device: DeviceProtocol, text: str) -> None:
    device.set_fastinput_ime(True)  # type: ignore[attr-defined]
    device.send_keys(text, clear=True)  # type: ignore[attr-defined]
    device.set_fastinput_ime(False)  # type: ignore[attr-defined]


app = create_phone_app()
