"""
多头 + 上涨 + 回踩 MA5 观察池（ma5_bull_pullback_watch）
纯观察：不触发买入，供自选与跟踪；可进化至 E-005 反包。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd

from theme_scan_utils import theme_symbols
from monthly_theme_dragon import DEFAULT_DB, DataStore, ScanResult, ThemeScanner, ThemeConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

ROOT = Path(__file__).resolve().parent
SETUP_TAG = "ma5_bull_pullback_watch"


@dataclass
class Ma5WatchConfig:
    ma5_touch_tolerance: float = 1.02  # 低 ≤ MA5×此值视为触及
    min_close_vs_ma5: float = 0.998  # 收 ≥ MA5×此值（允许极小幅假破）
    min_ret5: float = 0.0  # 5 日涨幅 > 此值
    ma5_trend_days: int = 20  # 近 N 日 low 不得跌破当日 MA5
    min_history: int = 60
    watch_top: int = 15


@dataclass
class Ma5WatchCandidate:
    symbol: str
    name: str
    close: float
    ma5: float
    ma10: float
    ma20: float
    ret5_pct: float
    dist_ma5_pct: float  # (收-MA5)/MA5 %
    in_active_theme: bool = False


@dataclass
class Ma5WatchScanResult:
    as_of: str
    scan_scope: str = "full_market"
    universe_size: int = 0
    theme_active: bool = False
    theme_name: str = ""
    candidates: List[Ma5WatchCandidate] = field(default_factory=list)


def _active_theme_symbol_set(
    store: DataStore, scan: Optional[ScanResult], daily: pd.DataFrame
) -> set:
    if not scan or not scan.theme_active:
        return set()
    return set(theme_symbols(store, scan, daily))


def _ma5_trend_intact(
    close: pd.Series, low: pd.Series, lookback: int
) -> bool:
    """近 lookback 日：每日最低价均未跌破当日 MA5（沿 5 日线趋势）。"""
    close = close.astype(float)
    low = low.astype(float)
    ma5 = close.rolling(5).mean()
    tail_idx = close.index[-lookback:]
    if len(tail_idx) < lookback:
        return False
    for d in tail_idx:
        m = ma5.loc[d]
        if pd.isna(m) or float(low.loc[d]) < float(m):
            return False
    return True


def _analyze_symbol(
    sub: pd.DataFrame, cfg: Ma5WatchConfig, as_of: pd.Timestamp
) -> Optional[dict]:
    sub = sub.sort_values("trade_date")
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < cfg.min_history:
        return None

    close = sub["close"].astype(float)
    low = sub["low"].astype(float)
    c = float(close.iloc[-1])
    l = float(low.iloc[-1])
    if c <= 0:
        return None

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])
    if any(pd.isna(x) for x in (ma5, ma10, ma20, ma60)):
        return None

    ret5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0.0

    ma_bull = ma5 > ma10 > ma20
    uptrend = c > ma20 and ma20 > ma60
    rising = ret5 > cfg.min_ret5
    touched = l <= ma5 * cfg.ma5_touch_tolerance
    hold_ma5 = c >= ma5 * cfg.min_close_vs_ma5
    ma5_trend = _ma5_trend_intact(close, low, cfg.ma5_trend_days)

    if not (ma_bull and uptrend and rising and touched and hold_ma5 and ma5_trend):
        return None

    dist_ma5 = (c - ma5) / ma5 if ma5 > 0 else 0.0
    name = str(sub["name"].iloc[-1] or sub["symbol"].iloc[-1])
    return {
        "symbol": str(sub["symbol"].iloc[-1]),
        "name": name,
        "close": c,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ret5_pct": ret5 * 100,
        "dist_ma5_pct": dist_ma5 * 100,
    }


def scan_ma5_bull_pullback_watch(
    store: DataStore,
    scan: Optional[ScanResult] = None,
    cfg: Optional[Ma5WatchConfig] = None,
    exclude_symbols: Optional[List[str]] = None,
    daily: Optional[pd.DataFrame] = None,
    quiet: bool = False,
) -> Ma5WatchScanResult:
    """全市场：多头排列 + 上涨趋势 + 当日回踩 MA5（观察，非可买）"""
    cfg = cfg or Ma5WatchConfig()
    exclude = set(exclude_symbols or [])
    as_of = (scan.as_of if scan else None) or store.latest_trade_date()
    if not as_of:
        return Ma5WatchScanResult(as_of="", scan_scope="full_market")

    if not quiet:
        logging.info("MA5 多头回踩观察：加载全市场行情 %s …", as_of)
    if daily is None:
        daily, _ = store.load_panel(as_of, lookback=90, symbols=None)
    else:
        daily = daily[daily["trade_date"] <= pd.Timestamp(as_of)].copy()
    if daily.empty:
        return Ma5WatchScanResult(as_of=as_of, scan_scope="full_market")

    as_of_ts = pd.Timestamp(as_of)
    theme_syms = _active_theme_symbol_set(store, scan, daily)
    scanned = daily["symbol"].nunique()
    rows: List[dict] = []

    for sym, grp in daily.groupby("symbol"):
        if sym in exclude:
            continue
        row = _analyze_symbol(grp, cfg, as_of_ts)
        if row:
            row["in_active_theme"] = sym in theme_syms
            rows.append(row)

    rows.sort(
        key=lambda r: (
            -int(r["in_active_theme"]),
            abs(r["dist_ma5_pct"]),
            -r["ret5_pct"],
        )
    )

    def _to_c(r: dict) -> Ma5WatchCandidate:
        return Ma5WatchCandidate(
            symbol=r["symbol"],
            name=r["name"],
            close=round(r["close"], 2),
            ma5=round(r["ma5"], 2),
            ma10=round(r["ma10"], 2),
            ma20=round(r["ma20"], 2),
            ret5_pct=round(r["ret5_pct"], 2),
            dist_ma5_pct=round(r["dist_ma5_pct"], 2),
            in_active_theme=bool(r.get("in_active_theme")),
        )

    return Ma5WatchScanResult(
        as_of=as_of,
        scan_scope="full_market",
        universe_size=scanned,
        theme_active=bool(scan and scan.theme_active),
        theme_name=scan.theme_name if scan and scan.theme_active else "",
        candidates=[_to_c(r) for r in rows[: cfg.watch_top]],
    )


def main():
    parser = argparse.ArgumentParser(description="MA5 多头回踩观察池扫描")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default=None)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    store = DataStore(Path(args.db))
    as_of = args.date or store.latest_trade_date()
    scan = ThemeScanner(store, ThemeConfig()).scan(as_of)
    cfg = Ma5WatchConfig(watch_top=args.top)
    result = scan_ma5_bull_pullback_watch(store, scan, cfg=cfg)
    print(f"信号日 {result.as_of} | 扫描 {result.universe_size} 只 | 观察 {len(result.candidates)} 只")
    for c in result.candidates:
        star = "★" if c.in_active_theme else " "
        print(
            f"  {star} {c.symbol} {c.name} 收{c.close} MA5={c.ma5} "
            f"5日{c.ret5_pct:+.1f}% 距MA5{c.dist_ma5_pct:+.2f}%"
        )


if __name__ == "__main__":
    main()
