"""出站校验：回复里声称做了物理动作，但本轮没有任何成功工具回执 → 重写/降级。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# (回复里的"我做了"说法, 允许支撑它的工具名)
_CLAIM_RULES: list[tuple[re.Pattern[str], set[str]]] = [
    (
        re.compile(r"购物车|加购|逛淘宝|刷淘宝|下单了|买到了"),
        {"phone_taobao_browse", "phone_prepare_order", "phone_do"},
    ),
    (
        re.compile(r"打开了|点开了|帮你点了|按了|输入了|发去了|发出去了"),
        {
            "phone_open_app",
            "phone_tap_text",
            "phone_type_text",
            "phone_back",
            "phone_do",
            "phone_prepare_order",
            "phone_send_wechat",
            "phone_send_qq",
        },
    ),
    (
        re.compile(r"发消息|发了消息|发了微信|发了QQ|说了|转了"),
        {"phone_send_wechat", "phone_send_qq", "send_message", "speak"},
    ),
    (
        re.compile(r"发了说说|换了头像|改了昵称|改了签名"),
        {"post_qzone", "set_avatar", "set_nickname", "set_signature"},
    ),
]

_FALLBACK_OFFLINE = "我现在连不上你的手机，做不了这个。"
_FALLBACK_NO_RECEIPT = "这个我没做成，不骗你。"


@dataclass(slots=True)
class ExecutionReceipt:
    tool: str
    ok: bool
    summary: str = ""


@dataclass(slots=True)
class ExecutionLedger:
    receipts: list[ExecutionReceipt] = field(default_factory=list)

    def record(self, tool_name: str, ok: bool, summary: str = "") -> None:
        self.receipts.append(ExecutionReceipt(tool=tool_name, ok=ok, summary=summary))

    def successful_tools(self) -> set[str]:
        return {receipt.tool for receipt in self.receipts if receipt.ok}

    def has_any_success(self) -> bool:
        return any(receipt.ok for receipt in self.receipts)


class ClaimGuard:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def violating_claim(self, text: str, ledger: ExecutionLedger) -> bool:
        successes = ledger.successful_tools()
        for pattern, allowed in _CLAIM_RULES:
            if pattern.search(text or "") and not (successes & allowed):
                return True
        return False

    async def enforce(
        self, text: str, ledger: ExecutionLedger, snapshot: Any, *, user_text: str = ""
    ) -> str:
        if not text or not self.violating_claim(text, ledger):
            return text
        log.warning("claim_guard_rewrite", text=text[:120], receipts=len(ledger.receipts))
        note = (
            "系统校验：你刚才的回复声称做了物理动作（打开 App/加购物车/发消息等），"
            "但本轮没有任何成功的工具回执。请改口，按【设备真实状态】如实回答；"
            "如果手机没连上，就说连不上；如果没调用工具，就说没真的做成。"
            "不要解释系统，也不要道歉、不要说对不起；一句话说清楚就行。"
        )
        rewritten = await self.agent.rewrite(text, note, user_text=user_text)
        if not rewritten:
            return (
                _FALLBACK_OFFLINE
                if not getattr(snapshot, "connected", False)
                else _FALLBACK_NO_RECEIPT
            )
        if self.violating_claim(rewritten, ledger):
            return (
                _FALLBACK_OFFLINE
                if not getattr(snapshot, "connected", False)
                else _FALLBACK_NO_RECEIPT
            )
        return rewritten
