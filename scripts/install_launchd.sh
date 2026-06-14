#!/bin/bash
# 安装 / 卸载 macOS launchd 定时任务（每个交易日 16:30 收盘后更新）
set -euo pipefail

PROJECT_DIR="/Users/guosixu/Documents/quant"
PLIST_SRC="${PROJECT_DIR}/scripts/com.quant.stock-daily.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.quant.stock-daily.plist"
LABEL="com.quant.stock-daily"

chmod +x "${PROJECT_DIR}/scripts/daily_stock_update.sh"

case "${1:-install}" in
    install)
        mkdir -p "${PROJECT_DIR}/logs"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
        launchctl enable "gui/$(id -u)/${LABEL}"
        echo "✅ 定时任务已安装: 每个交易日 17:00 执行"
        echo "   流程: daily_stock_update.sh → daily + retry + daily_briefing"
        echo "   plist: $PLIST_DST"
        echo "   日志: ${PROJECT_DIR}/logs/launchd_daily.log"
        echo ""
        echo "⚠️  必做：项目在「文稿/Documents」下，launchd 默认无权限读 .venv"
        echo "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加并勾选："
        echo "   1) /bin/bash"
        echo "   2) ${PROJECT_DIR}/.venv/bin/python"
        echo "   添加后执行:"
        echo "   launchctl kickstart -k gui/\$(id -u)/com.quant.stock-daily"
        echo "   诊断: ${PROJECT_DIR}/scripts/check_launchd_daily.sh"
        echo "   若仍失败，备用: scripts/install_user_cron.sh install"
        ;;
    uninstall)
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "✅ 定时任务已卸载"
        ;;
    status)
        launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null || echo "未安装"
        ;;
    *)
        echo "用法: $0 [install|uninstall|status]"
        exit 1
        ;;
esac
