#!/usr/bin/env bash
# 安卓无线调试一键配对 + 连接（Android 11+，OPPO ColorOS 14 实测路径）
#
# 用法:
#   scripts/phone-setup.sh pair <配对IP:端口> <6位配对码>
#   scripts/phone-setup.sh connect <连接IP:端口>
#   scripts/phone-setup.sh status
#
# 手机端：
#   设置 → 关于本机 → 版本信息 → 连点「版本号」7 次 → 开发者选项 → 无线调试
#   - 「使用配对码配对设备」弹窗里是【配对 IP:端口 + 6 位码】
#   - 无线调试主页面显示的是【连接用 IP:端口】（和配对端口不同）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HOME="$ROOT/.adb-home"
mkdir -p "$HOME"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ADB="$ROOT/tools/platform-tools/adb"
[[ -x "$ADB" ]] || { echo "没找到 adb: $ADB（先下载 platform-tools）"; exit 1; }

case "${1:-}" in
  pair)
    "$ADB" pair "${2:?配对 IP:端口}" "${3:?6 位配对码}"
    ;;
  connect)
    "$ADB" connect "${2:?连接 IP:端口}"
    "$ADB" devices -l
    ;;
  status)
    "$ADB" devices -l
    ;;
  *)
    echo "用法: $0 pair <配对IP:端口> <配对码> | connect <连接IP:端口> | status"
    exit 2
    ;;
esac
