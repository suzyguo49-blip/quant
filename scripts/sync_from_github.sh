#!/bin/bash
# Mac 端：拉取 GitHub 上手机 Agent 改过的 account.yaml → 重跑日报 → 推微信
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BRANCH="${1:-main}"
PYTHON="${PROJECT_DIR}/.venv/bin/python"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') git pull origin ${BRANCH} ====="
git pull origin "$BRANCH"

if [[ ! -f "${PROJECT_DIR}/stock_data.db" ]]; then
  echo "❌ 未找到 stock_data.db（仅在本机，不进 Git）。请先在本机跑 dataScrapper。"
  exit 1
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily briefing ====="
"$PYTHON" scripts/trading/daily_briefing.py

if [[ -f "${PROJECT_DIR}/docs/trading-system/notify.yaml" ]]; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') push briefing ====="
  "$PYTHON" scripts/trading/send_briefing.py || echo "推送失败（日报已生成）"
else
  echo "跳过推送：未配置 notify.yaml"
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') done ====="
