"""手机代理的 HTTP 客户端（bot 侧用）。"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class PhoneError(RuntimeError):
    """手机代理不可达/操作失败。"""


class PhoneClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise PhoneError(f"手机代理连不上：{exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:200]
            raise PhoneError(f"手机代理返回 {response.status_code}：{detail}")
        return response

    async def health(self) -> dict[str, Any]:
        return (await self._request("GET", "/health")).json()

    async def ui(self) -> dict[str, Any]:
        return (await self._request("GET", "/ui")).json()

    async def screenshot(self) -> bytes:
        return (await self._request("GET", "/screenshot")).content

    async def notifications(self) -> dict[str, Any]:
        return (await self._request("GET", "/notifications")).json()

    async def apps(self, all: bool = False) -> dict[str, Any]:
        return (await self._request("GET", "/apps", params={"all": all})).json()

    async def open_app(self, package: str) -> dict[str, Any]:
        return (await self._request("POST", "/open_app", json={"package": package})).json()

    async def tap_text(self, text: str, timeout: float = 5.0) -> dict[str, Any]:
        return (
            await self._request("POST", "/tap_text", json={"text": text, "timeout": timeout})
        ).json()

    async def type_text(self, text: str) -> dict[str, Any]:
        return (await self._request("POST", "/type_text", json={"text": text})).json()

    async def send_message(self, app: str, contact: str, text: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST", "/send_message", json={"app": app, "contact": contact, "text": text}
            )
        ).json()

    async def clear_text(self) -> dict[str, Any]:
        return (await self._request("POST", "/clear_text")).json()

    async def paste_text(self, text: str) -> dict[str, Any]:
        return (await self._request("POST", "/paste_text", json={"text": text})).json()

    async def tap_xy(self, x: int, y: int) -> dict[str, Any]:
        return (await self._request("POST", "/tap_xy", json={"x": x, "y": y})).json()

    async def wake(self) -> dict[str, Any]:
        return (await self._request("GET", "/wake")).json()

    async def back(self) -> dict[str, Any]:
        return (await self._request("POST", "/back")).json()

    async def stop(self) -> dict[str, Any]:
        return (await self._request("POST", "/stop")).json()

    async def resume(self) -> dict[str, Any]:
        return (await self._request("POST", "/resume")).json()
