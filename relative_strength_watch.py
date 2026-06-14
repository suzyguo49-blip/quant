"""
大盘下跌期 · 相对抗跌观察（relative_strength_watch / E-008）
规则：playbooks/notes.md N-501～N-503
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd

from monthly_theme_dragon import DEFAULT_DB, DataStore, ScanResult, ThemeScanner, ThemeConfig
from providers.pe_growth import PeGrowthGate
from theme_scan_utils import theme_symbols

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

SETUP_TAG = "relative_strength_watch"


@dataclass
class RsWatchConfig:
    min_index_ret5: float = -0.02  # 全市场 5 日 ≤ 此值才激活（默认 −2%）
    min_rs_spread: float = 0.03  # 个股 5 日须跑赢大盘 ≥ 3%
    min_stock_ret5: float = 0.0  # 个股 5 日涨幅 > 0
    min_history: int = 60
    watch_top: int = 15


@dataclass
class RsWatchCandidate:
    symbol: str
    name: str
    close: float
    ret5_pct: float
    index_ret5_pct: float
    rs_spread_pct: float
    ma5: float
    ma10: float
    ma20: float
    in_active_theme: bool = False


@dataclass
class RsWatchScanResult:
    as_of: str
    scan_scope: str = "full_market"
    universe_size: int = 0
    theme_active: bool = False
    theme_name: str = ""
    index_ret5_pct: float = 0.0
    regime_active: bool = False  # 是否处于「大盘下跌观察期」
    candidates: List[RsWatchCandidate] = field(default_factory=list)


def _is_index_symbol(sym: str) -> bool:
    return sym.startswith("sh.000") or sym.startswith("sz.399")


def compute_market_ret5(daily: pd.DataFrame, as_of: pd.Timestamp) -> float:
    """全 A 等权日收益复合，估算市场 5 日涨跌。"""
    sub = daily[(daily["trade_date"] <= as_of) & (~daily["isST"].fillna(0).astype(bool))].copy()
    sub = sub[~sub["symbol"].map(_is_index_symbol)]
    if sub.empty:
        return 0.0

    pct = pd.to_numeric(sub["pct_chg"], errors="coerce")
    close = sub["close"].astype(float)
    pre = sub["pre_close"].astype(float)
    sub["ret"] = pct / 100.0
    miss = sub["ret"].isna()
    sub.loc[miss, "ret"] = (close[miss] / pre[miss] - 1).where(pre[miss] > 0)

    by_date = sub.groupby("trade_date")["ret"].mean().sort_index()
    dates = [d for d in by_date.index if d <= as_of]
    if len(dates) < 5:
        return 0.0
    last5 = dates[-5:]
    compound = 1.0
    for d in last5:
        compound *= 1.0 + float(by_date[d])
    return compound - 1.0


def _active_theme_symbol_set(
    store: DataStore, scan: Optional[ScanResult], daily: pd.DataFrame
) -> set:
    if not scan or not scan.theme_active:
        return set()
    return set(theme_symbols(store, scan, daily))


def _analyze_symbol(
    sub: pd.DataFrame,
    cfg: RsWatchConfig,
    as_of: pd.Timestamp,
    index_ret5: float,
    pe_gate: PeGrowthGate,
) -> Optional[dict]:
    sub = sub.sort_values("trade_date")
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < cfg.min_history:
        return None
    if sub["isST"].fillna(0).astype(bool).iloc[-1]:
        return None

    close = sub["close"].astype(float)
    c = float(close.iloc[-1])
    if c <= 0:
        return None

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])
    if any(pd.isna(x) for x in (ma5, ma10, ma20, ma60)):
        return None

    ret5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0.0
    rs_spread = ret5 - index_ret5

    ma_bull = ma5 > ma10 > ma20
    uptrend = c > ma20 and ma20 > ma60
    if not (ma_bull and uptrend):
        return None
    if ret5 <= cfg.min_stock_ret5:
        return None
    if rs_spread < cfg.min_rs_spread:
        return None

    sym = str(sub["symbol"].iloc[-1])
    if not pe_gate.ok(sym):
        return None

    name = str(sub["name"].iloc[-1] or sym)
    return {
        "symbol": sym,
        "name": name,
        "close": c,
        "ret5_pct": ret5 * 100,
        "index_ret5_pct": index_ret5 * 100,
        "rs_spread_pct": rs_spread * 100,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
    }


def scan_relative_strength_watch(
    store: DataStore,
    scan: Optional[ScanResult] = None,
    cfg: Optional[RsWatchConfig] = None,
    exclude_symbols: Optional[List[str]] = None,
    daily: Optional[pd.DataFrame] = None,
    quiet: bool = False,
) -> RsWatchScanResult:
    """大盘下跌期：筛相对抗跌 + 趋势未坏的观察标的。"""
    cfg = cfg or RsWatchConfig()
    exclude = set(exclude_symbols or [])
    as_of = (scan.as_of if scan else None) or store.latest_trade_date()
    if not as_of:
        return RsWatchScanResult(as_of="", scan_scope="full_market")

    if not quiet:
        logging.info("相对抗跌观察：加载全市场行情 %s …", as_of)
    if daily is None:
        daily, _ = store.load_panel(as_of, lookback=90, symbols=None)
    else:
        daily = daily[daily["trade_date"] <= pd.Timestamp(as_of)].copy()
    if daily.empty:
        return RsWatchScanResult(as_of=as_of, scan_scope="full_market")

    as_of_ts = pd.Timestamp(as_of)
    index_ret5 = compute_market_ret5(daily, as_of_ts)
    regime_active = index_ret5 <= cfg.min_index_ret5
    theme_syms = _active_theme_symbol_set(store, scan, daily)
    scanned = daily[~daily["symbol"].map(_is_index_symbol)]["symbol"].nunique()
    pe_gate = PeGrowthGate(store.db_path, as_of)

    rows: List[dict] = []
    if regime_active:
        for sym, grp in daily.groupby("symbol"):
            if sym in exclude or _is_index_symbol(sym):
                continue
            row = _analyze_symbol(grp, cfg, as_of_ts, index_ret5, pe_gate)
            if row:
                row["in_active_theme"] = sym in theme_syms
                rows.append(row)

        rows.sort(
            key=lambda r: (
                -int(r["in_active_theme"]),
                -r["rs_spread_pct"],
                -r["ret5_pct"],
            )
        )

    def _to_c(r: dict) -> RsWatchCandidate:
        return RsWatchCandidate(
            symbol=r["symbol"],
            name=r["name"],
            close=round(r["close"], 2),
            ret5_pct=round(r["ret5_pct"], 2),
            index_ret5_pct=round(r["index_ret5_pct"], 2),
            rs_spread_pct=round(r["rs_spread_pct"], 2),
            ma5=round(r["ma5"], 2),
            ma10=round(r["ma10"], 2),
            ma20=round(r["ma20"], 2),
            in_active_theme=bool(r.get("in_active_theme")),
        )

    return RsWatchScanResult(
        as_of=as_of,
        scan_scope="full_market",
        universe_size=scanned,
        theme_active=bool(scan and scan.theme_active),
        theme_name=scan.theme_name if scan and scan.theme_active else "",
        index_ret5_pct=round(index_ret5 * 100, 2),
        regime_active=regime_active,
        candidates=[_to_c(r) for r in rows[: cfg.watch_top]],
    )


def main():
    parser = argparse.ArgumentParser(description="大盘下跌期相对抗跌观察池")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default=None)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    store = DataStore(Path(args.db))
    as_of = args.date or store.latest_trade_date()
    scan = ThemeScanner(store, ThemeConfig()).scan(as_of)
    cfg = RsWatchConfig(watch_top=args.top)
    result = scan_relative_strength_watch(store, scan, cfg=cfg)
    print(
        f"信号日 {result.as_of} | 市场5日 {result.index_ret5_pct:+.2f}% | "
        f"下跌观察期={'是' if result.regime_active else '否'} | "
        f"观察 {len(result.candidates)} 只"
    )
    for c in result.candidates:
        star = "★" if c.in_active_theme else " "
        print(
            f"  {star} {c.symbol} {c.name} 收{c.close} "
            f"5日{c.ret5_pct:+.1f}% 超额{c.rs_spread_pct:+.1f}%"
        )


if __name__ == "__main__":
    main()
