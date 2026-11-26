#!/usr/bin/env bash
# 启动手机代理：自动发现 ADB 设备（优先 mDNS 无线调试设备），再起 8765 服务
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HOME="$ROOT/.adb-home"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PATH="$ROOT/tools/platform-tools:$PATH"
ADB="$ROOT/tools/platform-tools/adb"
"$ADB" start-server >/dev/null 2>&1 || true
SERIAL="${DSD_PHONE_ADB_ADDRESS:-}"
if [ -z "$SERIAL" ]; then
  SERIAL="$("$ADB" devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1; exit}')"
fi
if [ -z "$SERIAL" ]; then
  echo "没有可用的 ADB 设备（请先开手机无线调试或插 USB）" >&2
  exit 1
fi
for pid in $(lsof -ti :8765 2>/dev/null || true); do kill -9 "$pid" 2>/dev/null || true; done
sleep 1
export UV_CACHE_DIR="$ROOT/.uv-cache"
exec "$(command -v uv || echo /opt/homebrew/bin/uv)" run uvicorn dsdaughter.phone.service:app \
  --host 127.0.0.1 --port 8765 --log-level info
