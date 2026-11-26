#!/usr/bin/env bash
# 干净地启动 bot：先清掉占用 8000 的旧进程，避免"旧进程还在服务、新代码没生效"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for pid in $(lsof -ti :8000 2>/dev/null || true); do kill -9 "$pid" 2>/dev/null || true; done
sleep 1
cd "$ROOT"
# launchd 的 PATH 很简陋：手动把常见路径和 uv 找出来
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export UV_CACHE_DIR="$ROOT/.uv-cache"
UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ]; then
  for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
    [ -x "$candidate" ] && UV_BIN="$candidate" && break
  done
fi
if [ -z "$UV_BIN" ]; then
  echo "找不到 uv，无法启动 bot" >&2
  exit 1
fi
exec "$UV_BIN" run uvicorn dsdaughter.main:app --host 0.0.0.0 --port 8000 --log-level info
