#!/usr/bin/env bash
# 一键把依赖栈拉起来：Docker → Postgres + NapCat → 检查端口
set -euo pipefail
echo "启动 Docker Desktop..."
open -a Docker 2>/dev/null || true
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then echo "Docker OK"; break; fi
  sleep 2
done
docker start dsdaughter-postgres-1 dsdaughter-napcat >/dev/null 2>&1 || true
sleep 8
docker ps --format '{{.Names}} {{.Status}}' | grep dsdaughter || true
( nc -z -G 3 127.0.0.1 5433 && echo "Postgres 5433 OK" ) || echo "Postgres 5433 未就绪"
