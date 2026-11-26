#!/usr/bin/env bash
# 锁屏/断连后重新连手机（无线调试）
#
# 用法：
#   scripts/phone-reconnect.sh                 # 只试 mDNS 自动发现
#   scripts/phone-reconnect.sh 192.168.110.101:40927   # 指定手机「IP 地址和端口」
# 连上后自动启动手机代理（8765）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HOME="$ROOT/.adb-home"
mkdir -p "$HOME"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ADB="$ROOT/tools/platform-tools/adb"

"$ADB" start-server >/dev/null 2>&1 || true
if [ $# -ge 1 ] && [ -n "${1:-}" ]; then
  echo "连接 $1 ..."
  "$ADB" connect "$1" || true
fi
sleep 2
SERIAL="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1; exit}')"
if [ -z "$SERIAL" ]; then
  echo "还没连上。请确认：手机亮屏解锁 → 开发者选项 → 无线调试 已开启"
  echo "然后把无线调试页面上的「IP 地址和端口」作为参数再跑一次："
  echo "  scripts/phone-reconnect.sh 192.168.110.101:端口"
  exit 1
fi
echo "已连接设备：$SERIAL"
for pid in $(lsof -ti :8765 2>/dev/null || true); do kill -9 "$pid" 2>/dev/null || true; done
sleep 1
echo "启动手机代理（8765）..."
nohup "$ROOT/scripts/phone-agent-start.sh" > /tmp/dsdaughter-phone-agent.log 2>&1 &
sleep 6
if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
  echo "手机代理 OK：$(curl -s http://127.0.0.1:8765/health)"
else
  echo "手机代理没起来，看日志：tail -50 /tmp/dsdaughter-phone-agent.log"
fi
