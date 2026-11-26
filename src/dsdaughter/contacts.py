"""她的人际关系：动态白名单（好友圈）+ 临时会话记录。

和"记忆"分开：
- 记忆是她记得的事；
- 这里是**权限**：谁能跟她说话、她能主动找谁。

设计取舍（用户明确要的"人的权限"）：
- 白名单不再写死在 .env，而是可以由她自己增删（有每日上限和审计）；
- 老爸（owner）永远在白名单里，且不能被删/不能被移出——这是硬保护；
- 陌生人的临时会话（群里点她头像发起的会话）可以聊，但不会自动变成好友；
- 她删掉的人可以从临时会话找回，被"哄"回好友列表（通过好友申请自动通过）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Contact:
    user_id: int
    note: str = ""
    source: str = "手动"  # 老爸 / 手动 / 临时会话 / 好友申请
    role: str = "friend"  # owner（老爸，固定）/ family（家人）/ friend
    added_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "note": self.note,
            "source": self.source,
            "role": self.role,
            "added_at": self.added_at,
        }


class ContactStore:
    """白名单持久化（JSON，原子写）+ 临时会话来源记录。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.path = Path(getattr(settings, "data_dir", "data")) / "contacts.json"
        self.owner: int = 0
        self._contacts: dict[int, Contact] = {}
        self._temp: dict[int, dict[str, Any]] = {}
        self._family_phone_authorized = False
        self._family_phone_authorized_at = ""
        self._family_recipients: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> None:
        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("联系人文件读取失败，从 .env 重新播种", error=str(exc))

        owner_raw = str(getattr(self.settings, "owner_qq", "") or "").strip()
        seeded = [
            int(token.strip())
            for token in str(getattr(self.settings, "qq_allowed_users", "") or "")
            .replace("，", ",")
            .split(",")
            if token.strip().isdigit()
        ]
        self.owner = int(owner_raw) if owner_raw.isdigit() else (seeded[0] if seeded else 0)

        for item in raw.get("contacts") or []:
            try:
                contact = Contact(
                    user_id=int(item["user_id"]),
                    note=str(item.get("note") or ""),
                    source=str(item.get("source") or "手动"),
                    role=str(item.get("role") or "friend"),
                    added_at=float(item.get("added_at") or time.time()),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._contacts[contact.user_id] = contact

        self._family_phone_authorized = bool(raw.get("family_phone_authorized"))
        self._family_phone_authorized_at = str(raw.get("family_phone_authorized_at") or "")
        for name, info in (raw.get("family_recipients") or {}).items():
            if isinstance(info, dict):
                self._family_recipients[str(name)] = info

        for user_id, info in (raw.get("temp") or {}).items():
            try:
                self._temp[int(user_id)] = {
                    "group_id": int(info.get("group_id") or 0) or None,
                    "last_at": float(info.get("last_at") or 0),
                }
            except (TypeError, ValueError):
                continue

        # 首次启动 / 文件里没有时，用 .env 播种
        if not self._contacts and seeded:
            for user_id in seeded:
                self._contacts[user_id] = Contact(
                    user_id=user_id,
                    note="老爸" if user_id == self.owner else "",
                    source="老爸" if user_id == self.owner else "env",
                )
            self._save()
        # owner 必须在名单里
        if self.owner and self.owner not in self._contacts:
            self._contacts[self.owner] = Contact(user_id=self.owner, note="老爸", source="老爸")
            self._save()

    def _save(self) -> None:
        payload = {
            "owner": self.owner,
            "family_phone_authorized": self._family_phone_authorized,
            "family_phone_authorized_at": self._family_phone_authorized_at,
            "family_recipients": self._family_recipients,
            "contacts": [contact.to_dict() for contact in self._contacts.values()],
            "temp": {str(user_id): info for user_id, info in self._temp.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp_path, self.path)

    # ---------------------------------------------------------------- 查询
    @property
    def allowed_users(self) -> set[int]:
        return set(self._contacts)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._contacts

    def get(self, user_id: int) -> Contact | None:
        return self._contacts.get(user_id)

    def list_contacts(self) -> list[Contact]:
        return sorted(
            self._contacts.values(),
            key=lambda item: (item.user_id != self.owner, item.added_at),
        )

    # ---------------------------------------------------------------- 修改
    def role_of(self, user_id: int) -> str:
        if user_id == self.owner:
            return "owner"
        contact = self._contacts.get(user_id)
        return contact.role if contact else "friend"

    def set_role(self, user_id: int, role: str) -> Contact:
        if role not in {"family", "friend"}:
            raise ValueError("role 只能是 family 或 friend")
        if user_id == self.owner:
            raise ValueError("老爸的角色固定在代码里，不能改")
        contact = self._contacts.get(user_id)
        if contact is None:
            raise ValueError("这个人还不在联系人里")
        contact.role = role
        self._save()
        log.info("联系人角色变更", user_id=user_id, role=role)
        return contact

    def allow(
        self, user_id: int, *, note: str = "", source: str = "手动", role: str = "friend"
    ) -> Contact:
        if user_id <= 0:
            raise ValueError("QQ 号不对")
        contact = self._contacts.get(user_id)
        if contact is None:
            contact = Contact(user_id=user_id, note=note, source=source, role=role)
            self._contacts[user_id] = contact
            log.info("已加进白名单", user_id=user_id, source=source)
        else:
            if note:
                contact.note = note
        self._save()
        return contact

    def disallow(self, user_id: int) -> bool:
        if user_id == self.owner:
            raise ValueError("老爸不能被移出白名单")
        removed = self._contacts.pop(user_id, None) is not None
        if removed:
            log.info("已移出白名单", user_id=user_id)
            self._save()
        return removed

    @property
    def family_phone_authorized(self) -> bool:
        return self._family_phone_authorized

    def authorize_family_phone(self) -> str:
        """老爸亲口授权家人用手机代发普通消息（一次性，持久化）。"""
        self._family_phone_authorized = True
        self._family_phone_authorized_at = time.strftime("%Y-%m-%d %H:%M")
        self._save()
        log.info("家人代发已获老爸授权", at=self._family_phone_authorized_at)
        return self._family_phone_authorized_at

    def authorize_family_recipient(self, name: str, *, any_content: bool = False) -> None:
        """老爸指定：家人可以代发消息给这个联系人；any_content=True 表示内容不限。"""
        key = str(name or "").strip()
        if not key:
            raise ValueError("收件人名字不能为空")
        self._family_recipients[key] = {
            "approved_at": time.strftime("%Y-%m-%d %H:%M"),
            "any_content": bool(any_content),
        }
        self._save()
        log.info("家人代发收件人已授权", name=key, any_content=any_content)

    @property
    def family_recipients(self) -> list[str]:
        return list(self._family_recipients)

    @staticmethod
    def _name_matches(authorized: str, actual: str) -> bool:
        """微信备注常带后缀（黄佳佳 8.25），做保守的前缀/包含匹配。"""
        left = "".join(str(authorized or "").split()).lower()
        right = "".join(str(actual or "").split()).lower()
        if not left or not right:
            return False
        if left == right:
            return True
        # 只允许"授权名是实际名的前缀/实际名包含授权名"，避免误发
        return right.startswith(left) or (len(left) >= 2 and left in right)

    def is_family_recipient(self, name: str) -> bool:
        return any(self._name_matches(item, name) for item in self._family_recipients)

    def family_recipient_any_content(self, name: str) -> bool:
        for item, info in self._family_recipients.items():
            if self._name_matches(item, name):
                return bool(info.get("any_content"))
        return False

    # ---------------------------------------------------------------- 临时会话
    def remember_temp(self, user_id: int, group_id: int | None) -> None:
        self._temp[user_id] = {"group_id": group_id, "last_at": time.time()}
        self._save()

    def temp_group(self, user_id: int) -> int | None:
        info = self._temp.get(user_id) or {}
        return info.get("group_id")

    def known_temp(self, user_id: int) -> bool:
        return user_id in self._temp

    def temp_contacts(self) -> dict[int, dict[str, Any]]:
        return dict(self._temp)
