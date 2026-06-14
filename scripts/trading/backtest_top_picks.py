#!/usr/bin/env python3
"""回测：Top 3 v4 组合模拟（Dragon 优先、分层仓位、T-005 反包 MA5）。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from MomentumTrend import scan_momentum_picks  # noqa: E402
from box_breakout import scan_box_breakout  # noqa: E402
from briefing_top_picks import (  # noqa: E402
    PickCandidate,
    TopPicksConfig,
    TopPicksState,
    build_top_picks,
    signal_priority,
)
from daily_briefing import DEFAULT_DB, ACCOUNT_PATH, load_account  # noqa: E402
from ma5_bull_pullback_watch import scan_ma5_bull_pullback_watch  # noqa: E402
from ma5_pullback_engulf import scan_ma5_pullback_engulf  # noqa: E402
from monthly_theme_dragon import DataStore, ThemeConfig, ThemeScanner  # noqa: E402
from relative_strength_watch import scan_relative_strength_watch  # noqa: E402
from tech_gentle_value import scan_tech_gentle_value  # noqa: E402

LOT = 100
COMMISSION = 0.001
MAX_POSITIONS = 3
DEPLOY_RATIO = 0.95
NO_DRAGON_DEPLOY_RATIO = 1 / 3
MIN_HOLD_DAYS_MA5 = 3
TRAIL_PROFIT_PCT = 8.0
OUT_DIR = ROOT / "docs/trading-system/backtests"


@dataclass
class ExitRule:
    stop_pct: float
    ma_exit: int
    use_ma5: bool = False
    min_hold_days_ma5: int = 0
    trail_profit_pct: float = TRAIL_PROFIT_PCT
    label: str = ""


@dataclass
class Position:
    symbol: str
    name: str
    shares: int
    entry_price: float
    entry_date: str
    entry_score: float
    current_score: float
    primary: str
    signals: List[str] = field(default_factory=list)


def _primary_signal(signals: List[str]) -> str:
    order = (
        "Dragon·可买",
        "MA5反包",
        "科技价值",
        "Dragon支线",
        "箱体突破",
    )
    for tag in order:
        if tag in signals:
            return tag
    return signals[0] if signals else "default"


def exit_rule_for_signal(primary: str) -> ExitRule:
    if primary == "MA5反包":
        return ExitRule(
            0.03, 20, True, MIN_HOLD_DAYS_MA5, TRAIL_PROFIT_PCT, "R-001+X-009/X-010+T-005"
        )
    if primary.startswith("Dragon"):
        return ExitRule(0.05, 20, False, 0, TRAIL_PROFIT_PCT, "R-003+X-003")
    if primary == "箱体突破":
        return ExitRule(0.05, 20, False, 0, TRAIL_PROFIT_PCT, "R-003+X-012")
    if primary == "科技价值":
        return ExitRule(0.05, 20, False, 0, TRAIL_PROFIT_PCT, "R-003+X-008")
    return ExitRule(0.05, 20, False, 0, TRAIL_PROFIT_PCT, "R-003+X-003")


def _has_dragon_buy(cands: List[PickCandidate]) -> bool:
    return any("Dragon·可买" in c.signals for c in cands)


def _max_positions_for_day(cands: List[PickCandidate], tp_cfg: TopPicksConfig) -> int:
    return MAX_POSITIONS if _has_dragon_buy(cands) else tp_cfg.no_dragon_max_slots


def _deploy_ratio_for_day(cands: List[PickCandidate]) -> float:
    return DEPLOY_RATIO if _has_dragon_buy(cands) else NO_DRAGON_DEPLOY_RATIO


def _hold_days(all_dates: List[str], entry_date: str, as_of: str) -> int:
    if entry_date not in all_dates or as_of not in all_dates:
        return 0
    i0 = all_dates.index(entry_date)
    i1 = all_dates.index(as_of)
    return max(0, i1 - i0)


def _load_trade_dates(db_path: Path, end: str, limit: int = 500) -> List[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily
            WHERE trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (end, limit),
        ).fetchall()
    return sorted(r[0] for r in rows)


def _price_row(panel: pd.DataFrame, sym: str, dt: str, col: str = "close") -> Optional[float]:
    ts = pd.Timestamp(dt)
    sub = panel[(panel["symbol"] == sym) & (panel["trade_date"] == ts)]
    if sub.empty:
        sub = panel[(panel["symbol"] == sym) & (panel["trade_date"].astype(str).str[:10] == dt)]
    if sub.empty:
        return None
    val = float(sub.iloc[-1][col])
    return val if np.isfinite(val) else None


def _ma_at(panel: pd.DataFrame, sym: str, dt: str, n: int) -> float:
    ts = pd.Timestamp(dt)
    sub = panel[(panel["symbol"] == sym) & (panel["trade_date"] <= ts)].sort_values("trade_date")
    closes = sub["close"].astype(float).tail(n)
    if len(closes) < n:
        return float("nan")
    return float(closes.mean())


def _lot_shares(budget: float, price: float) -> int:
    if price <= 0 or budget < price * LOT:
        return 0
    return int(budget / price / LOT) * LOT


def _load_full_panel(db_path: Path, start: str, end: str) -> pd.DataFrame:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(
            """
            SELECT d.symbol, d.name, d.trade_date, d.open, d.high, d.low,
                   d.close, d.pre_close, d.pct_chg, d.volume, d.amount,
                   d.turn, d.isST, d.pe, d.pe_ttm,
                   b.industry, b.ipo_date
            FROM stock_daily d
            LEFT JOIN stock_basic b ON d.symbol = b.code
            WHERE d.trade_date BETWEEN ? AND ?
            """,
            conn,
            params=(start, end),
        )


class CachedDataStore:
    def __init__(self, store: DataStore, panel: pd.DataFrame):
        self._store = store
        self._panel = panel.copy()
        self.db_path = store.db_path

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def load_panel(
        self, end_date: str, lookback: int = 80, symbols: Optional[List[str]] = None
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        dates = sorted(
            self._panel.loc[self._panel["trade_date"] <= end_date, "trade_date"].unique()
        )
        if not dates:
            return pd.DataFrame(), pd.DataFrame()
        use_dates = set(dates[-lookback:])
        df = self._panel[
            (self._panel["trade_date"].isin(use_dates))
            & (self._panel["trade_date"] <= end_date)
        ]
        if symbols:
            df = df[df["symbol"].isin(symbols)]
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        industry = (
            df[["symbol", "industry"]].drop_duplicates("symbol")
            if "industry" in df.columns
            else pd.DataFrame()
        )
        return df.copy(), industry


def run_top_picks_for_date(
    store: CachedDataStore,
    account: dict,
    signal_date: str,
    state: TopPicksState,
    trade_dates: List[str],
    tp_cfg: TopPicksConfig,
):
    max_pos = int(account.get("strategy_max_positions") or 4)
    cfg = ThemeConfig(max_dragons=max_pos)
    universe = store.theme_universe_symbols()
    scan = ThemeScanner(store, cfg, symbols=universe).scan(signal_date)
    exclude = list(account.get("long_term_symbols") or [])
    momentum = scan_momentum_picks(
        store.db_path,
        signal_date,
        top_stocks=int(account.get("momentum_top_stocks") or 4),
        exclude_symbols=exclude,
    )
    rs = scan_relative_strength_watch(store, scan, exclude_symbols=exclude)
    tech = scan_tech_gentle_value(store, signal_date, exclude_symbols=exclude)
    engulf = scan_ma5_pullback_engulf(store, scan, exclude_symbols=exclude)
    box = scan_box_breakout(store, scan, exclude_symbols=exclude)
    ma5w = scan_ma5_bull_pullback_watch(store, scan, exclude_symbols=exclude)
    result = build_top_picks(
        store,
        signal_date,
        scan,
        momentum,
        rs,
        tech,
        engulf,
        box,
        ma5w,
        top_n=MAX_POSITIONS,
        skip_news=True,
        cfg=tp_cfg,
        state=state,
        trade_dates=trade_dates,
    )
    state.update_after(signal_date, result.picks, momentum)
    mom_leader = momentum[0]["symbol"] if momentum else None
    return result, mom_leader


def _check_exit(
    pos: Position,
    panel: pd.DataFrame,
    dt: str,
    all_dates: List[str],
) -> Optional[str]:
    if pos.entry_date >= dt:
        return None
    px = _price_row(panel, pos.symbol, dt, "close")
    if px is None:
        return None
    rule = exit_rule_for_signal(pos.primary)
    pnl_pct = (px / pos.entry_price - 1) * 100
    hold = _hold_days(all_dates, pos.entry_date, dt)

    if pnl_pct <= -rule.stop_pct * 100:
        return "stop_loss"

    if pnl_pct >= rule.trail_profit_pct:
        if px <= pos.entry_price:
            return "trail_breakeven"
        ma5 = _ma_at(panel, pos.symbol, dt, 5)
        if np.isfinite(ma5) and px < ma5:
            return "trail_ma5"

    if rule.use_ma5 and hold >= rule.min_hold_days_ma5:
        ma5 = _ma_at(panel, pos.symbol, dt, 5)
        if np.isfinite(ma5) and px < ma5:
            return "ma5_break"
    ma = _ma_at(panel, pos.symbol, dt, rule.ma_exit)
    if np.isfinite(ma) and px < ma:
        return f"ma{rule.ma_exit}_break"
    return None


def _sell_position(
    pos: Position,
    panel: pd.DataFrame,
    dt: str,
    px: float,
    reason: str,
    trade_rows: List[dict],
) -> float:
    proceeds = pos.shares * px * (1 - COMMISSION)
    cost = pos.shares * pos.entry_price * (1 + COMMISSION)
    pnl_pct = (proceeds / cost - 1) * 100
    trade_rows.append(
        {
            "action": "sell",
            "symbol": pos.symbol,
            "name": pos.name,
            "primary_signal": pos.primary,
            "entry_date": pos.entry_date,
            "exit_date": dt,
            "entry_price": round(pos.entry_price, 3),
            "exit_price": round(px, 3),
            "shares": pos.shares,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_amount": round(proceeds - cost, 2),
            "exit_reason": reason,
            "entry_score": pos.entry_score,
            "win": pnl_pct > 0,
        }
    )
    return proceeds


def _buy_position(
    cand: PickCandidate,
    panel: pd.DataFrame,
    dt: str,
    budget: float,
    trade_rows: List[dict],
) -> Optional[Position]:
    px = _price_row(panel, cand.symbol, dt, "open")
    if px is None:
        px = _price_row(panel, cand.symbol, dt, "close")
    if px is None or px <= 0:
        return None
    shares = _lot_shares(budget, px)
    if shares <= 0:
        return None
    cost = shares * px * (1 + COMMISSION)
    primary = _primary_signal(cand.signals)
    trade_rows.append(
        {
            "action": "buy",
            "symbol": cand.symbol,
            "name": cand.name,
            "primary_signal": primary,
            "entry_date": dt,
            "exit_date": "",
            "entry_price": round(px, 3),
            "exit_price": "",
            "shares": shares,
            "pnl_pct": 0.0,
            "pnl_amount": round(-cost, 2),
            "exit_reason": "",
            "entry_score": cand.composite,
            "win": False,
        }
    )
    return Position(
        symbol=cand.symbol,
        name=cand.name,
        shares=shares,
        entry_price=px,
        entry_date=dt,
        entry_score=cand.composite,
        current_score=cand.composite,
        primary=primary,
        signals=list(cand.signals),
    )


def _portfolio_value(cash: float, positions: Dict[str, Position], panel: pd.DataFrame, dt: str) -> float:
    total = cash
    for pos in positions.values():
        px = _price_row(panel, pos.symbol, dt, "close")
        if px is not None:
            total += pos.shares * px
    return total


def _score_map(result, positions: Dict[str, Position]) -> Dict[str, float]:
    m = {p.symbol: p.composite for p in result.all_candidates}
    for sym, pos in positions.items():
        m[sym] = m.get(sym, pos.current_score)
        pos.current_score = m[sym]
    return m


def _picks_cache_path(end: str, days: int) -> Path:
    return OUT_DIR / f"top_picks_v4_cache_{days}d_{end}.json"


def _serialize_picks(picks) -> list:
    return [
        {
            "symbol": p.symbol,
            "name": p.name,
            "composite": p.composite,
            "signals": p.signals,
        }
        for p in picks
    ]


def _replay_state(state: TopPicksState, cached: dict, signal_dates: List[str]):
    for sd in signal_dates:
        if sd not in cached:
            break
        entry = cached[sd]
        picks = [
            PickCandidate(
                symbol=p["symbol"],
                name=p["name"],
                fund_score=0,
                tech_score=0,
                news_score=0,
                composite=p["composite"],
                fund_note="",
                tech_note="",
                news_note="",
                signals=p["signals"],
            )
            for p in entry.get("picks") or []
        ]
        mom = [{"symbol": entry["mom_leader"]}] if entry.get("mom_leader") else []
        state.update_after(sd, picks, mom)


def run_backtest(
    db_path: Path,
    signal_days: int = 20,
    end_date: Optional[str] = None,
    initial_capital: float = 100_000.0,
    use_cache: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    store = DataStore(db_path)
    account = load_account(ACCOUNT_PATH)
    end = end_date or store.latest_trade_date()
    if not end:
        raise SystemExit("数据库无交易日")

    all_dates = _load_trade_dates(db_path, end, limit=600)
    if len(all_dates) < signal_days + 2:
        raise SystemExit("交易日不足")

    signal_dates = all_dates[-(signal_days + 1) : -1]
    sim_start = all_dates[all_dates.index(signal_dates[0])]
    panel_start = all_dates[max(0, all_dates.index(sim_start) - 120)]
    full_panel = _load_full_panel(db_path, panel_start, end)
    if full_panel.empty:
        raise SystemExit("未加载行情")
    cached_store = CachedDataStore(store, full_panel)
    panel = full_panel.copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])

    tp_cfg = TopPicksConfig()
    state = TopPicksState()
    cache_path = _picks_cache_path(end, signal_days)
    cached: Dict[str, dict] = {}
    if use_cache and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        _replay_state(state, cached, signal_dates)

    # 预计算每日 Top3
    daily_picks: Dict[str, List[PickCandidate]] = {}
    daily_reason: Dict[str, str] = {}
    for sd in signal_dates:
        if sd in cached:
            daily_picks[sd] = [
                PickCandidate(
                    symbol=p["symbol"],
                    name=p["name"],
                    fund_score=0,
                    tech_score=0,
                    news_score=0,
                    composite=p["composite"],
                    fund_note="",
                    tech_note="",
                    news_note="",
                    signals=p["signals"],
                )
                for p in cached[sd].get("picks") or []
            ]
            daily_reason[sd] = cached[sd].get("empty_reason") or ""
        else:
            result, mom_leader = run_top_picks_for_date(
                cached_store, account, sd, state, all_dates, tp_cfg
            )
            daily_picks[sd] = list(result.picks)
            daily_reason[sd] = result.empty_reason
            cached[sd] = {
                "picks": _serialize_picks(result.picks),
                "empty_reason": result.empty_reason,
                "mom_leader": mom_leader,
            }
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")

    signal_to_entry = {sd: all_dates[all_dates.index(sd) + 1] for sd in signal_dates}

    cash = initial_capital
    positions: Dict[str, Position] = {}
    trade_rows: List[dict] = []
    daily_rows: List[dict] = []
    sim_dates = [d for d in all_dates if d >= sim_start and d <= end]

    for dt in sim_dates:
        actions: List[str] = []

        # 1) 收盘：止损 / 均线出场
        for sym in list(positions.keys()):
            reason = _check_exit(positions[sym], panel, dt, all_dates)
            if not reason:
                continue
            px = _price_row(panel, sym, dt, "close")
            if px is None:
                continue
            cash += _sell_position(positions[sym], panel, dt, px, reason, trade_rows)
            actions.append(f"卖{sym}({reason})")
            del positions[sym]

        # 2) 开盘：信号日=昨日 → 换仓 / 开仓
        prev_idx = all_dates.index(dt) - 1
        if prev_idx >= 0:
            prev = all_dates[prev_idx]
            if prev in signal_to_entry and signal_to_entry[prev] == dt:
                cands = daily_picks.get(prev, [])
                if not cands:
                    daily_rows.append(
                        {
                            "signal_date": prev,
                            "trade_date": dt,
                            "positions": len(positions),
                            "equity": round(_portfolio_value(cash, positions, panel, dt), 0),
                            "actions": "无信号 " + daily_reason.get(prev, ""),
                            "empty_reason": daily_reason.get(prev, ""),
                        }
                    )
                else:
                    scores = {c.symbol: c.composite for c in cands}
                    for sym in positions:
                        if sym in scores:
                            positions[sym].current_score = scores[sym]

                    max_pos = _max_positions_for_day(cands, tp_cfg)
                    deploy_ratio = _deploy_ratio_for_day(cands)
                    rot_gap = tp_cfg.rotation_min_score_gap

                    for cand in cands:
                        if cand.symbol in positions:
                            positions[cand.symbol].current_score = cand.composite
                            continue

                        equity = _portfolio_value(cash, positions, panel, dt)
                        slot_budget = equity * deploy_ratio / max(max_pos, 1)

                        if len(positions) < max_pos:
                            pos = _buy_position(cand, panel, dt, min(cash, slot_budget), trade_rows)
                            if pos:
                                cash -= pos.shares * pos.entry_price * (1 + COMMISSION)
                                positions[pos.symbol] = pos
                                actions.append(f"买{cand.symbol}({cand.composite:.0f})")
                            continue

                        if not positions:
                            continue
                        weakest = min(
                            positions.keys(),
                            key=lambda s: positions[s].current_score,
                        )
                        if cand.composite <= positions[weakest].current_score + rot_gap:
                            continue
                        px = _price_row(panel, weakest, dt, "open") or _price_row(
                            panel, weakest, dt, "close"
                        )
                        if px is None:
                            continue
                        cash += _sell_position(
                            positions[weakest], panel, dt, px, "rotation", trade_rows
                        )
                        actions.append(f"换出{weakest}({positions[weakest].current_score:.0f})")
                        del positions[weakest]
                        pos = _buy_position(cand, panel, dt, min(cash, slot_budget), trade_rows)
                        if pos:
                            cash -= pos.shares * pos.entry_price * (1 + COMMISSION)
                            positions[pos.symbol] = pos
                            actions.append(f"换入{cand.symbol}({cand.composite:.0f})")

                    daily_rows.append(
                        {
                            "signal_date": prev,
                            "trade_date": dt,
                            "positions": len(positions),
                            "equity": round(_portfolio_value(cash, positions, panel, dt), 0),
                            "actions": "；".join(actions) if actions else "持有不变",
                            "empty_reason": "",
                        }
                    )

    trades_df = pd.DataFrame(trade_rows)
    daily_df = pd.DataFrame(daily_rows)
    final_equity = _portfolio_value(cash, positions, panel, sim_dates[-1])
    summary = pd.DataFrame(
        [
            {
                "initial_capital": initial_capital,
                "final_equity": round(final_equity, 2),
                "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
                "open_positions": len(positions),
            }
        ]
    )
    return trades_df, daily_df, summary


def print_summary(
    trades: pd.DataFrame,
    daily: pd.DataFrame,
    summary: pd.DataFrame,
    signal_days: int,
) -> None:
    print("\n" + "=" * 60)
    print(f"Top 3 组合回测 v4（近 {signal_days} 个信号日）")
    print("=" * 60)

    if not summary.empty:
        s = summary.iloc[0]
        print(f"\n【组合】初始 {s['initial_capital']:,.0f} → 期末 {s['final_equity']:,.0f}  "
              f"（{s['total_return_pct']:+.2f}%）")
        print(f"  仍持仓: {int(s['open_positions'])} 只")

    sells = trades[trades["action"] == "sell"] if not trades.empty and "action" in trades.columns else pd.DataFrame()
    if sells.empty:
        print("\n无卖出成交")
    else:
        n = len(sells)
        wins = int(sells["win"].sum())
        print(f"\n【已平仓】{n} 笔，胜率 {wins/n*100:.1f}%，均收益 {sells['pnl_pct'].mean():+.2f}%")
        print(f"  总已实现盈亏: {sells['pnl_amount'].sum():+,.0f} 元")
        for reason, grp in sells.groupby("exit_reason"):
            print(f"  {reason}: {len(grp)} 笔, 均 {grp['pnl_pct'].mean():+.2f}%")

    print("\n【假设说明】")
    print("  · v4：Dragon·可买 优先；无 Dragon·可买 时最多 1 仓、总仓 ≤1/3")
    print("  · 反包须主线成分；5日≤12%；排除 ST；箱体须 Dragon 共振")
    print("  · 换仓：新分 > 持仓分 +5；反包 −3% 止损，满 3 日才破 MA5")
    print("  · Dragon/其他 −5%；浮盈 >8% 止损上移至成本/MA5")


def main():
    parser = argparse.ArgumentParser(description="Top3 v4 组合回测")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    trades, daily, summary = run_backtest(
        Path(args.db),
        signal_days=args.days,
        end_date=args.end,
        initial_capital=args.capital,
        use_cache=not args.refresh,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.end or "latest"
    tp = OUT_DIR / f"top_picks_v4_trades_{args.days}d_{tag}.csv"
    dp = OUT_DIR / f"top_picks_v4_daily_{args.days}d_{tag}.csv"
    sp = OUT_DIR / f"top_picks_v4_summary_{args.days}d_{tag}.csv"
    trades.to_csv(tp, index=False, encoding="utf-8-sig")
    daily.to_csv(dp, index=False, encoding="utf-8-sig")
    summary.to_csv(sp, index=False, encoding="utf-8-sig")
    print_summary(trades, daily, summary, args.days)
    print(f"\n成交: {tp}")
    print(f"每日: {dp}")
    print(f"汇总: {sp}")


if __name__ == "__main__":
    main()
