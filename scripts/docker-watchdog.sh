#!/usr/bin/env bash
# 看门狗：每 2 分钟检查 Docker daemon，掉了就尝试拉起（配合 LaunchAgent 使用）
set -u
LOG="${HOME}/Library/Logs/dsdaughter-docker-watchdog.log"
if docker info >/dev/null 2>&1; then
  exit 0
fi
mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T') docker down, starting..." >> "$LOG"
open -a Docker >/dev/null 2>&1 || true
