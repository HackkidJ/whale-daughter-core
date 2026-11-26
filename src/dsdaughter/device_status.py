"""真实设备状态：手机连没连上。注入 Prompt，并作为 ClaimGuard 的事实依据。"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from .phone.client import PhoneClient, PhoneError

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class DeviceSnapshot:
    connected: bool
    battery: int | None = None
    current_app: str = ""
    reason: str = ""
    checked_at: float = 0.0


class DeviceStatus:
    def __init__(self, settings, client_factory=None) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._snapshot: DeviceSnapshot | None = None
        self._cached_at = 0.0

    def _client(self) -> PhoneClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PhoneClient(
            getattr(self.settings, "phone_agent_url", "http://127.0.0.1:8765"),
            timeout=float(getattr(self.settings, "device_status_timeout", 2.0)),
        )

    async def probe(self, force: bool = False) -> DeviceSnapshot:
        ttl = float(getattr(self.settings, "device_status_ttl", 5.0))
        now = time.time()
        if not force and self._snapshot is not None and now - self._cached_at < ttl:
            return self._snapshot
        snapshot = DeviceSnapshot(connected=False, reason="手机代理无响应", checked_at=now)
        if not getattr(self.settings, "phone_enabled", False):
            snapshot.reason = "手机功能未启用"
        else:
            try:
                health = await self._client().health()
                snapshot = DeviceSnapshot(
                    connected=bool(health.get("ok")),
                    battery=health.get("battery"),
                    current_app=str(health.get("current_app") or ""),
                    reason="" if health.get("ok") else "手机代理返回异常",
                    checked_at=now,
                )
            except PhoneError as exc:
                snapshot.reason = f"手机未连接：{exc}"
            except Exception as exc:  # 探测失败一律视为未连接
                snapshot.reason = f"手机状态未知：{exc}"
        self._snapshot = snapshot
        self._cached_at = now
        return snapshot

    def prompt_block(self, snapshot: DeviceSnapshot) -> str:
        if not snapshot.connected:
            return (
                "【设备真实状态｜系统自动注入】\n"
                f"- 手机：未连接（{snapshot.reason}）\n"
                "- 因此你现在**无法**打开 App、点按、打字、发消息、逛淘宝、加购物车。\n"
                '- 对方让你做手机上的事，只能如实说"我现在连不上你的手机"；'
                '绝不允许说"我打开了/我加了购物车/我发了消息"。'
            )
        battery = f"{snapshot.battery}%" if snapshot.battery is not None else "未知"
        app = snapshot.current_app or "未知"
        return (
            "【设备真实状态｜系统自动注入】\n"
            f"- 手机：已连接，电量 {battery}，当前 App：{app}\n"
            '- 只有在工具返回成功回执后，才能说"我打开了/我加了购物车/我发了消息"；'
            "没调用工具、或工具失败，就必须如实说没做成。"
        )
