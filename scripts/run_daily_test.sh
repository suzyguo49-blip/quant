#!/bin/bash
# 手动测试定时任务同款流程（与 launchd 调用相同）
set -euo pipefail
PROJECT_DIR="/Users/guosixu/Documents/quant"
cd "$PROJECT_DIR"
exec ./scripts/daily_stock_update.sh
