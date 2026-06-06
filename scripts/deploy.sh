#!/bin/bash
# VPS Docker 部署腳本
# 使用方式：bash scripts/deploy.sh
set -e

# ── 1. 安裝 Docker（若尚未安裝）──────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "[1/3] 安裝 Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "[1/3] Docker 已安裝，跳過"
fi

# ── 2. 啟動服務 ───────────────────────────────────────────────────────────────
echo "[2/3] 啟動所有服務..."
docker compose pull caddy  2>/dev/null || true
docker compose up -d --build

# ── 3. 等待健康檢查 ───────────────────────────────────────────────────────────
echo "[3/3] 等待服務就緒..."
sleep 5
docker compose ps

echo ""
echo "✅ 完成！網址：https://lin-punch-system.crownai.ink"
echo "   查看 log：docker compose logs -f app"
