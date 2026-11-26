#!/usr/bin/env bash
# 直接改 Docker Desktop 配置把 AutoStart 打开（GUI 设置失败时的替代方案）
set -euo pipefail
SETTINGS="$HOME/Library/Group Containers/group.com.docker/settings-store.json"
if pgrep -x "Docker" >/dev/null 2>&1; then
  echo "请先退出 Docker Desktop（菜单栏 → Quit Docker Desktop）再运行本脚本"
  exit 1
fi
[[ -f "$SETTINGS" ]] || { echo "找不到 $SETTINGS"; exit 1; }
cp "$SETTINGS" "$SETTINGS.bak"
python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["AutoStart"] = True
data.setdefault("OpenUIOnStartupDisabled", True)
json.dump(data, open(path, "w"), indent=2)
print("AutoStart ->", data.get("AutoStart"))
PY
echo "完成（原配置备份为 settings-store.json.bak）。下次登录 macOS 会自动启动 Docker。"
echo "再启动：open -a Docker"
