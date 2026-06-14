#!/bin/bash
# 诊断 launchd 定时爬取是否可用
set -euo pipefail

PROJECT_DIR="/Users/guosixu/Documents/quant"
LABEL="com.quant.stock-daily"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

echo "=== launchd 状态 ==="
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E "state|runs|last exit|program|arguments" || echo "未安装"

echo ""
echo "=== plist ==="
if [[ -f "$PLIST" ]]; then
  plutil -p "$PLIST" | grep -E "ProgramArguments|StartCalendarInterval|StandardError"
else
  echo "缺失: $PLIST"
  echo "请运行: ${PROJECT_DIR}/scripts/install_launchd.sh install"
fi

echo ""
echo "=== 最近 stderr（权限问题看这里）==="
tail -5 "${PROJECT_DIR}/logs/launchd_stderr.log" 2>/dev/null || echo "(无)"

echo ""
echo "=== 最近 daily 日志 ==="
tail -8 "${PROJECT_DIR}/logs/launchd_daily.log" 2>/dev/null || echo "(无)"

echo ""
echo "=== venv 可读性（launchd 视角）==="
if "$PROJECT_DIR/.venv/bin/python" -c "import baostock; print('python OK')" 2>/dev/null; then
  echo "当前 shell: python OK"
else
  echo "当前 shell: python 失败"
fi

echo ""
echo "手动触发（与 17:00 相同流程）:"
echo "  launchctl kickstart -k gui/\$(id -u)/${LABEL}"
