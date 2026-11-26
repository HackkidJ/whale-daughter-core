#!/usr/bin/env bash
# 存档 NapCat 镜像与登录态。
#
# 背景：NapCatQQ 仓库已于 2026-11-12 清空（761 个 tag 与 release 全删），
# 镜像只剩 Docker Hub 上的 v4.18.42。这是目前唯一还能稳定拿到这个版本的方式，
# 所以**第一次部署就先跑这个脚本**，避免哪天镜像也被下架。
#
# 用法：./scripts/archive-napcat.sh [输出目录]
set -euo pipefail

IMAGE="mlikiowa/napcat-docker:v4.18.42"
# 国内直连 Docker Hub 经常超时，自动回退到镜像加速源（可用 NAPCAT_MIRROR 覆盖）
MIRROR="${NAPCAT_MIRROR:-docker.m.daocloud.io}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/deploy/data/backups}"
mkdir -p "$OUT"

if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "==> 本地已有 ${IMAGE}，跳过拉取"
elif ! docker pull "$IMAGE"; then
  echo "==> 直连 Docker Hub 失败，改用加速源：$MIRROR"
  docker pull "$MIRROR/$IMAGE"
  docker tag "$MIRROR/$IMAGE" "$IMAGE"
fi

ARCHIVE="$OUT/napcat-v4.18.42.tar"
echo "==> 导出镜像 → $ARCHIVE"
docker save "$IMAGE" -o "$ARCHIVE"

if command -v zstd >/dev/null 2>&1; then
  zstd -19 -T0 -f "$ARCHIVE" -o "$ARCHIVE.zst"
  rm -f "$ARCHIVE"
  ARCHIVE="$ARCHIVE.zst"
elif command -v gzip >/dev/null 2>&1; then
  gzip -9 -f "$ARCHIVE"
  ARCHIVE="$ARCHIVE.gz"
fi

echo "==> 计算校验和"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"
else
  shasum -a 256 "$ARCHIVE" | tee "$ARCHIVE.sha256"
fi

QQ_DIR="$ROOT/deploy/data/napcat/QQ"
if [ -d "$QQ_DIR" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  BACKUP="$OUT/napcat-qq-login-$STAMP.tar.gz"
  echo "==> 备份登录态 → $BACKUP"
  tar -czf "$BACKUP" -C "$ROOT/deploy/data/napcat" QQ
else
  echo "==> 还没有登录态目录（首次扫码登录后，再跑一次本脚本备份 QQ 目录）"
fi

echo
echo "完成。恢复镜像：docker load -i $ARCHIVE"
echo "备份目录：$OUT"
