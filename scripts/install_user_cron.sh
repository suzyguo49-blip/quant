#!/bin/bash
# 备用：用户 crontab 定时（当 launchd 无法访问「文稿」时使用）
set -euo pipefail

PROJECT_DIR="/Users/guosixu/Documents/quant"
MARK="# com.quant.stock-daily"
CRON_LINE="0 17 * * 1-5 /bin/bash -lc '${PROJECT_DIR}/scripts/daily_stock_update.sh' # com.quant.stock-daily"

case "${1:-install}" in
  install)
    tmp=$(mktemp)
    crontab -l 2>/dev/null | grep -v "$MARK" | grep -v "com.quant.stock-daily" >"$tmp" || true
    echo "$CRON_LINE" >>"$tmp"
    crontab "$tmp"
    rm -f "$tmp"
    echo "✅ 已写入 crontab（周一至五 17:00）"
    crontab -l | grep -F "com.quant.stock-daily" || true
    ;;
  uninstall)
    tmp=$(mktemp)
    crontab -l 2>/dev/null | grep -v "$MARK" | grep -v "com.quant.stock-daily" >"$tmp" || true
    crontab "$tmp" 2>/dev/null || true
    rm -f "$tmp"
    echo "✅ 已移除 crontab 条目"
    ;;
  *)
    echo "用法: $0 [install|uninstall]"
    exit 1
    ;;
esac
