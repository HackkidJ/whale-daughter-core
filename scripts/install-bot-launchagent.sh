#!/usr/bin/env bash
# 安装 bot 的 LaunchAgent：登录自启 + 崩溃自动重启
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/local.dsdaughter.bot.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__BOTSTART__|$ROOT/scripts/bot-start.sh|" \
  "$ROOT/deploy/launchd/local.dsdaughter.bot.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装：$PLIST"
echo "日志：/tmp/dsdaughter-bot.out /tmp/dsdaughter-bot.err"
echo "卸载：launchctl bootout gui/$(id -u) $PLIST && rm $PLIST"
