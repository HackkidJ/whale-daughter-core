#!/usr/bin/env bash
# 手机代理一键启动：adb 连接 + 起服务（前台运行，Ctrl-C 停）
# 用法: scripts/phone-up.sh 192.168.110.101:40005
# 手机重启/无线调试关闭后，先重新配对：scripts/phone-setup.sh pair <配对IP:端口> <码>
set -euo pipefail
ADDR="${1:?用法: $0 <连接IP:端口>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HOME="$ROOT/.adb-home"
"$ROOT/tools/platform-tools/adb" connect "$ADDR"
echo "启动手机代理（端口 8765）..."
cd "$ROOT"
export UV_CACHE_DIR="$ROOT/.uv-cache"
DSD_PHONE_ADB_ADDRESS="$ADDR" exec uv run uvicorn dsdaughter.phone.service:app --host 127.0.0.1 --port 8765 --log-level info
