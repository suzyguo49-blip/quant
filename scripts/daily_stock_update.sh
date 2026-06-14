#!/bin/bash
# A股收盘后：全量日线补全 + 交易日报（launchd / cron / 手动均可）
# 补数在后台跑；覆盖率达标或进程卡死/超时后仍继续生成日报，避免整晚无推送。
set -uo pipefail

PROJECT_DIR="/Users/guosixu/Documents/quant"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
export QUANT_DATA_SOURCE="${QUANT_DATA_SOURCE:-tushare}"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/launchd_daily.log"
MAX_RETRY_PASSES=3
# 补数最长等待（秒）；超时后若覆盖率已够仍生成日报
DAILY_MAX_WAIT="${DAILY_MAX_WAIT:-9000}"
# 覆盖率计数 N 分钟不增长视为卡死
DAILY_STALL_MINUTES="${DAILY_STALL_MINUTES:-30}"
POLL_INTERVAL="${POLL_INTERVAL:-60}"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

exec >>"$LOG_FILE" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily full update (pid $$) ====="

# 长任务防休眠（与 dataScrapper 内 caffeinate 双保险）
/usr/bin/caffeinate -dims &
CAFF_PID=$!
DAILY_PID=""
cleanup() {
  kill "$CAFF_PID" 2>/dev/null || true
  if [[ -n "$DAILY_PID" ]] && kill -0 "$DAILY_PID" 2>/dev/null; then
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') cleanup: stop background daily pid $DAILY_PID ====="
    kill "$DAILY_PID" 2>/dev/null || true
    wait "$DAILY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

coverage_status() {
  "$PYTHON" - <<'PY'
from dataScrapper import make_collector
from datetime import datetime
import os

c = make_collector(db_path=os.path.join(os.getcwd(), "stock_data.db"))
latest = c.get_latest_trade_date() or ""
min_ok = c.min_coverage_count(0.92)
n = c.count_symbols_on_date(latest) if latest else 0
expected = datetime.now().strftime("%Y-%m-%d")
print(f"{latest}|{n}|{min_ok}|{1 if n >= min_ok else 0}|{expected}")
PY
}

expected_signal_date() {
  "$PYTHON" - <<'PY'
from datetime import datetime
print(datetime.now().strftime("%Y-%m-%d"))
PY
}

# macOS 默认 bash 3.2 不支持 [[ a >= b ]]，用字典序比较 YYYY-MM-DD
date_ge() {
  [[ -n "$1" && -n "$2" && ( "$1" == "$2" || ! "$1" < "$2" ) ]]
}

date_lt() {
  [[ -n "$1" && -n "$2" && "$1" < "$2" ]]
}

run_retry_if_needed() {
  local latest="$1"
  local pass
  for pass in $(seq 1 "$MAX_RETRY_PASSES"); do
    IFS='|' read -r _ n min_ok ok _ <<< "$(coverage_status)"
    if [[ "$ok" == "1" ]]; then
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') coverage OK: ${n}/${min_ok} @ ${latest} ====="
      return 0
    fi
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') retry pass ${pass}/${MAX_RETRY_PASSES} (${n}/${min_ok}) ====="
    "$PYTHON" dataScrapper.py retry-failed --start "$latest" --end "$latest" || true
  done
  IFS='|' read -r _ n min_ok ok _ <<< "$(coverage_status)"
  if [[ "$ok" == "1" ]]; then
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') coverage OK after retry: ${n}/${min_ok} ====="
    return 0
  fi
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') coverage FAILED: ${n}/${min_ok} @ ${latest} ====="
  return 1
}

stop_daily_if_running() {
  local reason="$1"
  if [[ -n "$DAILY_PID" ]] && kill -0 "$DAILY_PID" 2>/dev/null; then
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') stop background daily ($reason) pid=$DAILY_PID ====="
    kill "$DAILY_PID" 2>/dev/null || true
    wait "$DAILY_PID" 2>/dev/null || true
    DAILY_PID=""
  fi
}

wait_daily_or_coverage() {
  local start_ts last_n last_progress_ts now elapsed stall latest n min_ok ok expected
  start_ts=$(date +%s)
  last_n=0
  last_progress_ts=$start_ts
  expected=$(expected_signal_date)

  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily in background (max_wait=${DAILY_MAX_WAIT}s stall=${DAILY_STALL_MINUTES}m expected=${expected}) ====="
  "$PYTHON" dataScrapper.py daily &
  DAILY_PID=$!
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily pid=$DAILY_PID ====="

  while true; do
    IFS='|' read -r latest n min_ok ok _ <<< "$(coverage_status)"
    now=$(date +%s)
    elapsed=$((now - start_ts))

    # 须等「当日信号日」入库且覆盖率达标，不能因昨日已达标就提前杀 daily
    if [[ "$ok" == "1" && -n "$latest" ]] && date_ge "$latest" "$expected"; then
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') coverage sufficient: ${n}/${min_ok} @ ${latest} (expected<=${expected}) ====="
      stop_daily_if_running "coverage OK on expected date"
      return 0
    fi

    if [[ "$ok" == "1" && -n "$latest" ]] && date_lt "$latest" "$expected"; then
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') waiting new trade_date: db=${latest} expected>=${expected} (${n}/${min_ok}) ====="
    fi

    if [[ "$n" -gt "$last_n" ]]; then
      last_n=$n
      last_progress_ts=$now
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily progress: ${n}/${min_ok} @ ${latest} ====="
    fi

    if [[ -n "$DAILY_PID" ]] && ! kill -0 "$DAILY_PID" 2>/dev/null; then
      wait "$DAILY_PID" || true
      DAILY_PID=""
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily process exited (${n}/${min_ok}) ====="
      return 0
    fi

    stall=$((now - last_progress_ts))
    if [[ "$stall" -ge $((DAILY_STALL_MINUTES * 60)) ]]; then
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily stalled ${DAILY_STALL_MINUTES}m (${n}/${min_ok}) ====="
      stop_daily_if_running "stalled"
      return 0
    fi

    if [[ "$elapsed" -ge "$DAILY_MAX_WAIT" ]]; then
      echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily max wait reached (${n}/${min_ok}) ====="
      stop_daily_if_running "timeout"
      return 0
    fi

    sleep "$POLL_INTERVAL"
  done
}

wait_daily_or_coverage

IFS='|' read -r latest n min_ok ok expected <<< "$(coverage_status)"
expected="${expected:-$(expected_signal_date)}"
if [[ -n "$latest" ]] && date_lt "$latest" "$expected"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') catch-up daily-quick (db=${latest} expected=${expected}) ====="
  "$PYTHON" dataScrapper.py daily-quick || true
  IFS='|' read -r latest n min_ok ok expected <<< "$(coverage_status)"
fi

if [[ -z "$latest" ]]; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') no trade_date in DB, abort ====="
  exit 1
fi

if ! run_retry_if_needed "$latest"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') skip briefing (data incomplete) ====="
  exit 1
fi

if date_lt "$latest" "$expected"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') skip briefing: latest=${latest} < expected=${expected} (非交易日或补数失败) ====="
  exit 1
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily briefing (signal_date=${latest}) ====="
if ! "$PYTHON" scripts/trading/daily_briefing.py --date "$latest"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') briefing FAILED ====="
  exit 1
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') notify (feishu + wechat) ====="
if [[ -f "${PROJECT_DIR}/docs/trading-system/notify.yaml" ]]; then
  if ! "$PYTHON" scripts/trading/send_briefing.py --date "$latest"; then
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') notify FAILED (briefing still saved) ====="
  fi
else
  echo "跳过推送：未配置 docs/trading-system/notify.yaml（见 notify.yaml.example）"
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') done ====="
