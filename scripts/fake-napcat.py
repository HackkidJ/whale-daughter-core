#!/usr/bin/env python3
"""假 NapCat：不登录 QQ，直接模拟 OneBot 反向 WS，用来联调 QQ 通道。

用法（先启动 bot）：
    python scripts/fake-napcat.py --token smoke --user 123 --text "在吗"
    python scripts/fake-napcat.py --group 456 --text "在吗"   # 模拟群里 @ 她

它会打印 bot 真实下发的 send_private_msg / send_group_msg 内容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

SELF_ID = 999


async def run(args: argparse.Namespace) -> int:
    url = args.url
    if args.token:
        url += f"?access_token={args.token}"

    async with websockets.connect(url) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": SELF_ID,
                    "status": {},
                    "interval": 30000,
                }
            )
        )

        segments: list[dict] = [{"type": "text", "data": {"text": args.text}}]
        if args.group is not None:
            segments.insert(0, {"type": "at", "data": {"qq": str(SELF_ID)}})
        is_temp = args.temp_group is not None
        payload = {
            "post_type": "message",
            "message_type": "group" if args.group is not None else "private",
            "sub_type": "group" if is_temp else "",
            # 每次给不同的 message_id，否则会被通道层的去重拦住
            "message_id": int(time.time() * 1000) % 1_000_000,
            "self_id": SELF_ID,
            "user_id": args.user,
            "sender": {"nickname": args.nickname},
            "message": segments,
        }
        if args.group is not None:
            payload["group_id"] = args.group
        if is_temp:
            payload["group_id"] = args.temp_group

        started = time.monotonic()
        print(f"→ 发送事件：{args.text!r}（user={args.user} group={args.group}）")
        await websocket.send(json.dumps(payload))

        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
            data = json.loads(raw)
            if "action" not in data:
                continue
            elapsed = time.monotonic() - started
            params = data.get("params") or {}
            await websocket.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 1},
                        "echo": data.get("echo"),
                    }
                )
            )
            if data["action"] == "set_input_status":
                print(f"[{elapsed:5.2f}s] 正在输入…（event_type={params.get('event_type')}）")
                continue
            text = "".join(
                str(segment.get("data", {}).get("text", ""))
                for segment in params.get("message") or []
                if segment.get("type") == "text"
            )
            print(
                f"[{elapsed:5.2f}s] ← {data['action']} → {params.get('user_id')}"
                f"（group_id={params.get('group_id')}）"
            )
            print(text)
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟 NapCat 反向 WS，联调 QQ 通道")
    parser.add_argument(
        "--url", default="ws://127.0.0.1:8000/onebot/v11/ws", help="bot 的反向 WS 地址"
    )
    parser.add_argument("--token", default="", help="与 DSD_QQ_TOKEN 一致")
    parser.add_argument("--user", type=int, default=123, help="发消息的 QQ 号")
    parser.add_argument("--group", type=int, default=None, help="群号（不填=私聊）")
    parser.add_argument(
        "--temp-group",
        type=int,
        default=None,
        help="模拟临时会话：来源群号（private + sub_type=group）",
    )
    parser.add_argument("--nickname", default="老爸")
    parser.add_argument("--text", default="在吗")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待回复秒数")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except TimeoutError:
        print("× 等待回复超时（检查 bot 是否在跑、token 是否一致）", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"× 连接失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # websockets 被服务端拒绝时抛的是 InvalidStatus
        print(f"× 连接被拒绝：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("  提示：token 与 .env 里的 DSD_QQ_TOKEN 不一致，或 bot 没在跑", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
