#!/usr/bin/env bash
# 安装 Docker 看门狗 LaunchAgent：每 2 分钟检查，掉线自动 `open -a Docker`
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/local.dsdaughter.dockerwatch.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__WATCHDOG__|$ROOT/scripts/docker-watchdog.sh|" \
  "$ROOT/deploy/launchd/local.dsdaughter.dockerwatch.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装：$PLIST"
echo "日志：~/Library/Logs/dsdaughter-docker-watchdog.log"
echo "卸载：launchctl bootout gui/$(id -u) $PLIST && rm $PLIST"
