"""视觉操作循环：截图 → 视觉模型决定下一步 → 执行 → 再截图。

不依赖 App 的无障碍控件树（微信就是屏蔽了无障碍），完全靠"看屏幕"。
"""

from __future__ import annotations

import asyncio
import json
import re

import structlog

from ..vision import VisionClient, VisionError
from .client import PhoneClient, PhoneError

log = structlog.get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.S)

_PROMPT = """你在操作一部 Android 手机。截图尺寸是 {image_width}x{image_height} 像素。

目标：{task}

已经做过的步骤：
{history}

请只输出一个 JSON 对象（不要 markdown、不要解释），表示下一步动作：
- 点击：{{"action":"tap","x":整数,"y":整数,"reason":"..."}}
- 粘贴文字到当前输入框：{{"action":"paste","text":"要粘贴的内容","reason":"..."}}
- 返回键：{{"action":"back","reason":"..."}}
- 清空当前输入框：{{"action":"clear","reason":"..."}}
- 等待页面加载：{{"action":"wait","seconds":2,"reason":"..."}}
- 完成：{{"action":"done","summary":"完成了什么"}}
- 失败：{{"action":"failed","reason":"为什么做不了"}}

坐标用截图像素坐标。一次只输出一步。中文内容一律用 paste，不要模拟打字。
每一步都必须根据**当前这张截图**重新判断位置，不要沿用你上一步猜的坐标；不确定就输出 failed。
如果是微信发消息，顺序是：底部「微信」tab → 右上角放大镜 → 搜索框粘贴联系人 →
点搜索结果里名字匹配的联系人 → **先点一下底部输入框让它获得焦点** → 粘贴消息 →
**确认输入框里出现文字后** → 右下角绿色「发送」→ 截图确认消息气泡。
注意：粘贴消息前，如果输入框里已经有文字（可能是上次没发出去留下的），先输出 clear 清空，
再 paste；已经粘贴过就不要重复粘贴，去找发送按钮。
如果 paste 之后截图里输入框仍然是空的，说明粘贴没生效：再点一次输入框再 paste 一次。
重要：如果上一步刚粘贴完消息，下一步应该在输入框右侧/右下角找「发送」按钮并点击；
点完后再截一张图，看到自己发出的消息气泡才能输出 done。
"""


class PhoneTaskError(RuntimeError):
    """视觉操作没完成。"""


def _parse_action(raw: str) -> dict:
    cleaned = (raw or "").replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise PhoneTaskError(f"视觉模型没给 JSON：{raw[:120]}")
    try:
        action = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise PhoneTaskError(f"视觉模型 JSON 坏了：{raw[:120]}") from exc
    if not isinstance(action, dict) or "action" not in action:
        raise PhoneTaskError("视觉模型 JSON 缺少 action")
    return action


async def run_phone_task(
    client: PhoneClient,
    vision: VisionClient,
    task: str,
    *,
    max_steps: int = 12,
) -> str:
    history: list[str] = []
    for _step in range(max_steps):
        try:
            png = await client.screenshot()
        except PhoneError as exc:
            raise PhoneTaskError(f"截图失败：{exc}") from exc
        history_text = "\n".join(history) if history else "（还没有动作）"
        # task/history 先替换，剩下的 {image_width}/{image_height} 交给 vision.ask 格式化
        prompt = _PROMPT.replace("{task}", task).replace("{history}", history_text)
        try:
            raw, sent, original = await vision.ask(png, prompt, max_tokens=900)
        except VisionError as exc:
            raise PhoneTaskError(f"视觉模型失败：{exc}") from exc
        try:
            action = _parse_action(raw)
        except PhoneTaskError as exc:
            # 模型偶尔输出被截断/带 markdown，提醒它重来一步，不要直接失败
            history.append(
                f"上一步输出不是合法 JSON（{exc}），请只输出一行 JSON，reason 不超过 15 字"
            )
            log.warning("phone_vision_bad_json", raw=raw[:80])
            continue
        kind = str(action.get("action"))
        if kind == "done":
            return str(action.get("summary") or "完成")
        if kind == "failed":
            raise PhoneTaskError(str(action.get("reason") or "视觉模型认为做不到"))
        if kind == "tap":
            try:
                scale_x = original[0] / max(1, sent[0])
                scale_y = original[1] / max(1, sent[1])
                x = int(float(action["x"]) * scale_x)
                y = int(float(action["y"]) * scale_y)
            except (KeyError, TypeError, ValueError) as exc:
                raise PhoneTaskError("视觉模型给的坐标不对") from exc
            if not (0 <= x <= original[0] and 0 <= y <= original[1]):
                raise PhoneTaskError(f"坐标超出屏幕：{x},{y}")
            await client.tap_xy(x, y)
            history.append(f"点了 ({x},{y})：{action.get('reason', '')}")
        elif kind == "paste":
            text = str(action.get("text") or "")
            if not text:
                raise PhoneTaskError("视觉模型要粘贴空内容")
            await client.paste_text(text)
            history.append(f"粘贴了「{text}」")
        elif kind == "back":
            await client.back()
            history.append("按了返回")
        elif kind == "clear":
            await client.clear_text()
            history.append("清空了输入框")
        elif kind == "wait":
            seconds = min(5.0, max(0.5, float(action.get("seconds") or 2)))
            await asyncio.sleep(seconds)
            history.append(f"等了 {seconds} 秒让页面加载")
        else:
            raise PhoneTaskError(f"不认识的 action：{kind}")
        log.info("phone_vision_step", action=kind, history=history[-1:])
        await asyncio.sleep(1.2)
    raise PhoneTaskError(f"试了 {max_steps} 步还没完成，最后一步：{history[-1:]}")
