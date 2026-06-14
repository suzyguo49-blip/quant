"""
箱体震荡突破（box_range_breakout / Darvas 箱型理论）
规则：playbooks/notes.md N-401～N-404

箱体定义（对齐业界共识）：
- 箱顶/箱底为整理期内多次触碰的水平阻力/支撑（各 ≥2 次）
- 整理期缩量（均量 ≤ 突破前 20 日均量 × 阈值）
- 有效突破 = 收盘价站稳箱顶 + 放量（相对整理期均量显著放大）
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from monthly_theme_dragon import DEFAULT_DB, DataStore, ScanResult, ThemeScanner, ThemeConfig
from providers.pe_growth import PeGrowthGate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

ROOT = Path(__file__).resolve().parent
SETUP_TAG = "box_range_breakout"


@dataclass
class BoxConfig:
    box_min_days: int = 10
    box_max_days: int = 60
    box_min_width_pct: float = 0.04
    box_max_width_pct: float = 0.18
    min_close_inside_ratio: float = 0.80
    touch_tolerance: float = 0.015
    min_top_touches: int = 2
    min_bottom_touches: int = 2
    max_box_vol_vs_pre20: float = 0.92
    max_inbox_trend_pct: float = 0.12
    breakout_buffer: float = 1.001
    min_vol_vs_box_avg: float = 1.5
    min_vol_vs_avg5: float = 1.2
    min_body_ratio: float = 1.005
    min_close_in_range: float = 0.55
    max_upper_shadow: float = 0.50
    near_top_pct: float = 0.03
    min_pullback_from_120d_high: float = 0.08
    min_history: int = 80
    signal_top: int = 4
    watch_top: int = 6
    stop_loss_pct: float = 0.02
    ma_exit: int = 20


@dataclass
class BoxCandidate:
    symbol: str
    name: str
    close: float
    signal: str  # breakout | watch
    box_high: float
    box_low: float
    box_days: int
    box_width_pct: float
    top_touches: int
    bottom_touches: int
    vol_vs_box_avg: float
    vol_vs_avg5: float
    body_ratio: float
    ma20: float
    in_active_theme: bool = False
    gaps: List[str] = field(default_factory=list)


@dataclass
class BoxScanResult:
    as_of: str
    scan_scope: str = "full_market"
    universe_size: int = 0
    theme_active: bool = False
    theme_name: str = ""
    candidates_breakout: List[BoxCandidate] = field(default_factory=list)
    candidates_watch: List[BoxCandidate] = field(default_factory=list)


def _active_theme_symbol_set(
    store: DataStore, scan: Optional[ScanResult], daily: pd.DataFrame
) -> set:
    if not scan or not scan.theme_active:
        return set()
    from theme_scan_utils import theme_symbols

    return set(theme_symbols(store, scan, daily))


def _touch_count(series: pd.Series, level: float, tol_pct: float, side: str) -> int:
    tol = level * tol_pct
    if side == "top":
        return int((series >= level - tol).sum())
    return int((series <= level + tol).sum())


def _find_box(sub: pd.DataFrame, cfg: BoxConfig) -> Optional[dict]:
    """信号日前寻找有效箱体：多次触顶/触底 + 缩量横盘。"""
    if len(sub) < cfg.box_min_days + 22:
        return None

    best: Optional[dict] = None
    for n_days in range(cfg.box_max_days, cfg.box_min_days - 1, -1):
        window = sub.iloc[-(n_days + 1) : -1]
        if len(window) < cfg.box_min_days:
            continue

        high = window["high"].astype(float)
        low = window["low"].astype(float)
        close = window["close"].astype(float)
        vol = window["volume"].astype(float)

        box_high = float(high.max())
        box_low = float(low.min())
        mid = (box_high + box_low) / 2
        if mid <= 0 or box_high <= box_low:
            continue

        width_pct = (box_high - box_low) / mid
        if width_pct < cfg.box_min_width_pct or width_pct > cfg.box_max_width_pct:
            continue

        top_t = _touch_count(high, box_high, cfg.touch_tolerance, "top")
        bot_t = _touch_count(low, box_low, cfg.touch_tolerance, "bottom")
        if top_t < cfg.min_top_touches or bot_t < cfg.min_bottom_touches:
            continue

        inside = ((close >= box_low * 0.998) & (close <= box_high * 1.002)).mean()
        if inside < cfg.min_close_inside_ratio:
            continue

        inbox_trend = abs(float(close.iloc[-1]) - float(close.iloc[0])) / mid
        if inbox_trend > min(cfg.max_inbox_trend_pct, width_pct * 0.85):
            continue

        box_vol = float(vol.mean())
        pre = sub.iloc[-(n_days + 21) : -(n_days + 1)]["volume"].astype(float)
        pre_vol = float(pre.mean()) if len(pre) >= 10 else box_vol
        vol_ratio_pre = box_vol / pre_vol if pre_vol > 0 else 1.0
        if vol_ratio_pre > cfg.max_box_vol_vs_pre20:
            continue

        cand = {
            "box_high": box_high,
            "box_low": box_low,
            "box_days": n_days,
            "width_pct": width_pct,
            "inside_ratio": float(inside),
            "top_touches": top_t,
            "bottom_touches": bot_t,
            "box_vol_avg": box_vol,
            "vol_vs_pre20": vol_ratio_pre,
        }
        if best is None or n_days > best["box_days"]:
            best = cand
    return best


def _position_ok(sub: pd.DataFrame, cfg: BoxConfig, close: float, breakout: bool) -> tuple[bool, str]:
    high120 = float(sub["high"].astype(float).tail(120).max())
    if high120 <= 0:
        return True, ""
    dist = (high120 - close) / high120
    if breakout and close >= high120 * 0.998:
        return True, ""
    if dist < cfg.min_pullback_from_120d_high:
        return False, f"距120日高点仅{dist*100:.1f}%（需≥{cfg.min_pullback_from_120d_high*100:.0f}%回撤或有效突破新高）"
    return True, ""


def _analyze_symbol(
    sub: pd.DataFrame, cfg: BoxConfig, as_of: pd.Timestamp
) -> Optional[dict]:
    sub = sub.sort_values("trade_date")
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < cfg.min_history:
        return None

    close_s = sub["close"].astype(float)
    opn = sub["open"].astype(float)
    high = sub["high"].astype(float)
    low = sub["low"].astype(float)
    vol = sub["volume"].astype(float)

    c = float(close_s.iloc[-1])
    o = float(opn.iloc[-1])
    h = float(high.iloc[-1])
    l = float(low.iloc[-1])
    v = float(vol.iloc[-1])
    if c <= 0 or o <= 0:
        return None

    box = _find_box(sub, cfg)
    if not box:
        return None

    box_high = box["box_high"]
    box_low = box["box_low"]
    box_vol = box["box_vol_avg"]
    vol_vs_box = v / box_vol if box_vol > 0 else 0.0
    vol5 = float(vol.tail(6).iloc[:-1].mean()) if len(vol) >= 6 else float(vol.iloc[:-1].mean())
    vol_vs_avg5 = v / vol5 if vol5 > 0 else 0.0
    body_ratio = c / o
    ma20 = float(close_s.rolling(20).mean().iloc[-1])
    if pd.isna(ma20):
        return None

    breakout = c > box_high * cfg.breakout_buffer
    pos_ok, pos_gap = _position_ok(sub, cfg, c, breakout)
    yang = c > o
    vol_box_ok = vol_vs_box >= cfg.min_vol_vs_box_avg
    vol5_ok = vol_vs_avg5 >= cfg.min_vol_vs_avg5
    body_ok = body_ratio >= cfg.min_body_ratio
    rng = h - l
    close_pos = (c - l) / rng if rng > 1e-8 else 1.0
    close_pos_ok = close_pos >= cfg.min_close_in_range
    upper_shadow = (h - c) / rng if rng > 1e-8 else 0.0
    shadow_ok = upper_shadow <= cfg.max_upper_shadow

    prev_c = float(close_s.iloc[-2]) if len(close_s) >= 2 else c
    fresh_break = prev_c <= box_high * cfg.breakout_buffer

    near_top = (
        not breakout
        and c >= box_high * (1 - cfg.near_top_pct)
        and c <= box_high * cfg.breakout_buffer
    )

    gaps: List[str] = []
    if not pos_ok:
        gaps.append(pos_gap)
    if not breakout:
        gaps.append(f"收盘未站稳箱顶{box_high:.2f}（收{c:.2f}，须>{box_high * cfg.breakout_buffer:.2f}）")
    elif not fresh_break:
        gaps.append("前日已在箱顶之上（非新鲜突破）")
    if not yang:
        gaps.append("当日非阳线")
    if not vol_box_ok:
        gaps.append(f"相对整理期量能不足（今/箱均={vol_vs_box:.2f}，需≥{cfg.min_vol_vs_box_avg:.1f}）")
    if not vol5_ok:
        gaps.append(f"相对5均量不足（今/5均={vol_vs_avg5:.2f}，需≥{cfg.min_vol_vs_avg5:.1f}）")
    if not body_ok:
        gaps.append(f"实体偏弱（收/开={body_ratio:.3f}）")
    if not close_pos_ok:
        gaps.append(f"收盘偏低位（日内位置{close_pos:.0%}，需≥{cfg.min_close_in_range:.0%}）")
    if not shadow_ok:
        gaps.append(f"上影过长（{upper_shadow:.0%}）")

    is_breakout = (
        pos_ok
        and breakout
        and fresh_break
        and yang
        and vol_box_ok
        and vol5_ok
        and body_ok
        and close_pos_ok
        and shadow_ok
    )
    is_watch = pos_ok and near_top and yang and not is_breakout

    if not is_breakout and not is_watch:
        return None

    name = str(sub["name"].iloc[-1] or sub["symbol"].iloc[-1])
    score = (
        (c / box_high - 1) * 300
        + vol_vs_box * 20
        + box["box_days"] * 0.8
        + box["top_touches"]
        + box["bottom_touches"]
    )
    return {
        "symbol": str(sub["symbol"].iloc[-1]),
        "name": name,
        "close": c,
        "signal": "breakout" if is_breakout else "watch",
        "box_high": box_high,
        "box_low": box_low,
        "box_days": box["box_days"],
        "box_width_pct": box["width_pct"],
        "top_touches": box["top_touches"],
        "bottom_touches": box["bottom_touches"],
        "vol_vs_box_avg": vol_vs_box,
        "vol_vs_avg5": vol_vs_avg5,
        "body_ratio": body_ratio,
        "ma20": ma20,
        "gaps": gaps if not is_breakout else [],
        "score": score,
    }


def scan_box_breakout(
    store: DataStore,
    scan: Optional[ScanResult] = None,
    cfg: Optional[BoxConfig] = None,
    exclude_symbols: Optional[List[str]] = None,
    daily: Optional[pd.DataFrame] = None,
    quiet: bool = False,
) -> BoxScanResult:
    """全市场 A 股扫描箱体突破；scan 用于标注 Dragon 主线成分"""
    cfg = cfg or BoxConfig()
    exclude = set(exclude_symbols or [])
    as_of = (scan.as_of if scan else None) or store.latest_trade_date()
    if not as_of:
        return BoxScanResult(as_of="", scan_scope="full_market")

    if not quiet:
        logging.info("箱体震荡突破：加载全市场行情 %s …", as_of)
    if daily is None:
        daily, _ = store.load_panel(as_of, lookback=140, symbols=None)
    else:
        daily = daily[daily["trade_date"] <= pd.Timestamp(as_of)].copy()
    if daily.empty:
        return BoxScanResult(as_of=as_of, scan_scope="full_market")

    as_of_ts = pd.Timestamp(as_of)
    theme_syms = _active_theme_symbol_set(store, scan, daily)
    pe_gate = PeGrowthGate(store.db_path, as_of)
    scanned = daily["symbol"].nunique()
    signals: List[dict] = []
    watches: List[dict] = []

    for sym, grp in daily.groupby("symbol"):
        if sym in exclude:
            continue
        row = _analyze_symbol(grp, cfg, as_of_ts)
        if not row:
            continue
        row["in_active_theme"] = sym in theme_syms
        if row["signal"] == "breakout":
            if not pe_gate.ok(sym):
                row["gaps"] = list(row.get("gaps") or []) + [PeGrowthGate.GAP]
                watches.append(row)
            else:
                signals.append(row)
        else:
            watches.append(row)

    def _sort_key(r: dict):
        return (r["in_active_theme"], r["score"])

    signals.sort(key=_sort_key, reverse=True)
    watches.sort(key=_sort_key, reverse=True)

    def _to_c(r: dict) -> BoxCandidate:
        return BoxCandidate(
            symbol=r["symbol"],
            name=r["name"],
            close=round(r["close"], 2),
            signal=r["signal"],
            box_high=round(r["box_high"], 2),
            box_low=round(r["box_low"], 2),
            box_days=int(r["box_days"]),
            box_width_pct=round(r["box_width_pct"] * 100, 2),
            top_touches=int(r["top_touches"]),
            bottom_touches=int(r["bottom_touches"]),
            vol_vs_box_avg=round(r["vol_vs_box_avg"], 2),
            vol_vs_avg5=round(r["vol_vs_avg5"], 2),
            body_ratio=round(r["body_ratio"], 3),
            ma20=round(r["ma20"], 2),
            in_active_theme=bool(r.get("in_active_theme")),
            gaps=r.get("gaps") or [],
        )

    return BoxScanResult(
        as_of=as_of,
        scan_scope="full_market",
        universe_size=scanned,
        theme_active=bool(scan and scan.theme_active),
        theme_name=scan.theme_name if scan and scan.theme_active else "",
        candidates_breakout=[_to_c(r) for r in signals[: cfg.signal_top]],
        candidates_watch=[_to_c(r) for r in watches[: cfg.watch_top]],
    )


def print_scan(result: BoxScanResult, cfg: Optional[BoxConfig] = None):
    cfg = cfg or BoxConfig()
    print("\n" + "=" * 60)
    print(f"箱体震荡突破 {result.as_of}  setup_tag={SETUP_TAG}")
    print(f"范围: 全市场 A 股 {result.universe_size} 只")
    if result.theme_active:
        print(f"当日 Dragon 主线: {result.theme_name}（表中 ★=主线成分）")
    else:
        print("当日无 Dragon 主线")
    print("\n【突破·可买】")
    if not result.candidates_breakout:
        print("  （无）")
    else:
        for c in result.candidates_breakout:
            star = "★" if c.in_active_theme else ""
            print(
                f"  - {star}{c.symbol} {c.name} | 收{c.close:.2f} "
                f"箱顶{c.box_high:.2f} 箱底{c.box_low:.2f} "
                f"{c.box_days}日 触顶{c.top_touches}/底{c.bottom_touches} "
                f"量/箱均{c.vol_vs_box_avg:.2f}x"
            )
    print("\n【箱体观察·近上沿待突破】")
    if not result.candidates_watch:
        print("  （无）")
    else:
        for c in result.candidates_watch[:5]:
            gap = "；".join(c.gaps[:2]) if c.gaps else "—"
            print(
                f"  - {c.symbol} {c.name} | 收{c.close:.2f} "
                f"箱顶{c.box_high:.2f} {c.box_days}日 | 缺：{gap}"
            )
    print("=" * 60)


LOT = 100


def _ma_at(sub: pd.DataFrame, dt: pd.Timestamp, period: int) -> float:
    tail = sub[sub["trade_date"] <= dt].tail(period)["close"].astype(float)
    if len(tail) < period:
        return float("nan")
    return float(tail.mean())


def _hold_trading_days(trade_dates: list, entry: str, exit: str) -> int:
    ed, xd = pd.Timestamp(entry), pd.Timestamp(exit)
    days = [d for d in trade_dates if ed < pd.Timestamp(d) <= xd]
    return len(days) if days else 1


def _lot_shares(budget: float, price: float) -> int:
    if price <= 0 or budget <= 0:
        return 0
    return int(budget / price / LOT) * LOT


def _price_at(panel: pd.DataFrame, sym: str, dt: pd.Timestamp, col: str = "close") -> float:
    sub = panel[(panel["symbol"] == sym) & (panel["trade_date"] == dt)]
    if sub.empty:
        return float("nan")
    return float(sub.iloc[-1][col])


def _exit_reason(
    sub: pd.DataFrame,
    dt: pd.Timestamp,
    px: float,
    entry_price: float,
    box_low: float,
    cfg: BoxConfig,
) -> Optional[str]:
    pnl_pct = (px / entry_price - 1) * 100
    if pnl_pct <= -cfg.stop_loss_pct * 100:
        return "stop_loss"
    if px < box_low:
        return "box_low_break"
    ma20 = _ma_at(sub, dt, cfg.ma_exit)
    if ma20 == ma20 and px < ma20:
        return "ma20_break"
    return None


def run_box_backtest(
    db_path: Path,
    start: str,
    end: str,
    cfg: Optional[BoxConfig] = None,
    initial_cash: float = 100_000.0,
    invest_ratio: float = 0.95,
    commission: float = 0.001,
    save_csv: bool = True,
    tag_suffix: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    箱体突破回测（setup_tag=box_range_breakout）
    信号日收盘 scan → 次日开盘买入；T+1；
    出场：R-001 止损 / X-011 破箱底 / X-012 破 MA20。
    """
    cfg = cfg or BoxConfig()
    store = DataStore(db_path)
    daily_all, _ = store.load_panel(end, lookback=140, symbols=None)
    if daily_all.empty:
        logging.error("未加载到行情")
        return pd.DataFrame(), pd.DataFrame()

    name_map = (
        daily_all[["symbol", "name"]]
        .drop_duplicates("symbol")
        .set_index("symbol")["name"]
        .astype(str)
        .to_dict()
    )
    n_syms = daily_all["symbol"].nunique()
    logging.info("全市场 %s 只，预加载行情 %s 条", n_syms, len(daily_all))

    trade_dates = sorted(
        daily_all.loc[
            (daily_all["trade_date"] >= pd.Timestamp(start))
            & (daily_all["trade_date"] <= pd.Timestamp(end)),
            "trade_date",
        ].unique()
    )
    if not trade_dates:
        logging.error("回测区间无交易日")
        return pd.DataFrame(), pd.DataFrame()

    cash = initial_cash
    positions: Dict[str, dict] = {}
    pending_buys: List[dict] = []
    daily_rows: List[dict] = []
    trade_rows: List[dict] = []
    prev_total = initial_cash
    signal_cache: Dict[str, List[dict]] = {}

    for i, dt in enumerate(trade_dates):
        dt_str = pd.Timestamp(dt).strftime("%Y-%m-%d")
        actions: List[str] = []

        if pending_buys and i > 0:
            deploy_base = cash + sum(
                positions[s]["shares"] * _price_at(daily_all, s, dt, "close")
                for s in positions
                if _price_at(daily_all, s, dt, "close") == _price_at(daily_all, s, dt, "close")
            )
            deploy = deploy_base * invest_ratio
            per_slot = deploy / cfg.signal_top if cfg.signal_top else deploy
            for sig in pending_buys:
                sym = sig["symbol"]
                if sym in positions or len(positions) >= cfg.signal_top:
                    continue
                px = _price_at(daily_all, sym, dt, "open")
                if px != px or px <= 0:
                    px = _price_at(daily_all, sym, dt, "close")
                if px != px or px <= 0:
                    continue
                shares = _lot_shares(per_slot, px)
                cost = shares * px * (1 + commission)
                if shares <= 0 or cost > cash:
                    continue
                cash -= cost
                positions[sym] = {
                    "shares": shares,
                    "entry_price": px,
                    "entry_date": dt_str,
                    "box_low": sig["box_low"],
                }
                actions.append(f"买 {sym} {shares}@{px:.2f}")
            pending_buys = []

        to_sell: List[str] = []
        for sym, pos in list(positions.items()):
            if pd.Timestamp(dt_str) <= pd.Timestamp(pos["entry_date"]):
                continue
            px = _price_at(daily_all, sym, dt, "close")
            if px != px:
                continue
            sub = daily_all[daily_all["symbol"] == sym]
            reason = _exit_reason(
                sub, dt, px, pos["entry_price"], pos["box_low"], cfg
            )
            if reason:
                proceeds = pos["shares"] * px * (1 - commission)
                cash += proceeds
                pnl = (px / pos["entry_price"] - 1) * 100
                hold = _hold_trading_days(
                    [pd.Timestamp(d).strftime("%Y-%m-%d") for d in trade_dates],
                    pos["entry_date"],
                    dt_str,
                )
                trade_rows.append({
                    "symbol": sym,
                    "name": name_map.get(sym, sym),
                    "entry_date": pos["entry_date"],
                    "exit_date": dt_str,
                    "hold_days": hold,
                    "entry_price": pos["entry_price"],
                    "exit_price": px,
                    "pnl_pct": pnl,
                    "exit_reason": reason,
                    "setup_tag": SETUP_TAG,
                })
                actions.append(f"卖 {sym} {reason} {pnl:+.1f}%")
                to_sell.append(sym)
        for sym in to_sell:
            del positions[sym]

        if dt_str not in signal_cache:
            panel = daily_all[daily_all["trade_date"] <= dt].copy()
            sigs: List[dict] = []
            for sym, grp in panel.groupby("symbol"):
                row = _analyze_symbol(grp, cfg, pd.Timestamp(dt))
                if row and row["signal"] == "breakout":
                    sigs.append(row)
            sigs.sort(key=lambda r: r["score"], reverse=True)
            signal_cache[dt_str] = sigs[: cfg.signal_top]

        if i < len(trade_dates) - 1:
            for sig in signal_cache.get(dt_str, []):
                if sig["symbol"] not in positions and len(positions) + len(pending_buys) < cfg.signal_top:
                    pending_buys.append(sig)

        mkt = sum(
            positions[s]["shares"] * _price_at(daily_all, s, dt, "close")
            for s in positions
            if _price_at(daily_all, s, dt, "close") == _price_at(daily_all, s, dt, "close")
        )
        total = cash + mkt
        daily_rows.append({
            "date": dt_str,
            "cash": cash,
            "market_value": mkt,
            "total": total,
            "return_pct": (total / prev_total - 1) * 100 if prev_total else 0,
            "positions": len(positions),
            "actions": "; ".join(actions),
        })
        prev_total = total

    journal = pd.DataFrame(daily_rows)
    trades = pd.DataFrame(trade_rows)

    if save_csv and not journal.empty:
        out_dir = ROOT / "docs/trading-system/backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"_{tag_suffix}" if tag_suffix else ""
        jpath = out_dir / f"box_breakout_{start.replace('-','')}_{end.replace('-','')}{tag}_journal.csv"
        tpath = out_dir / f"box_breakout_{start.replace('-','')}_{end.replace('-','')}{tag}_trades.csv"
        journal.to_csv(jpath, index=False)
        trades.to_csv(tpath, index=False)
        _write_backtest_md(
            out_dir / f"box_breakout_{start.replace('-','')}_{end.replace('-','')}{tag}.md",
            start, end, cfg, journal, trades,
        )
        logging.info("已保存 %s", jpath)

    if not journal.empty:
        ret = (journal.iloc[-1]["total"] / initial_cash - 1) * 100
        n = len(trades)
        wr = (trades["pnl_pct"] > 0).mean() * 100 if n else 0
        logging.info(
            "回测 %s~%s | 收益 %.2f%% | %d 笔 | 胜率 %.1f%%",
            start, end, ret, n, wr,
        )
    return journal, trades


def _write_backtest_md(
    path: Path,
    start: str,
    end: str,
    cfg: BoxConfig,
    journal: pd.DataFrame,
    trades: pd.DataFrame,
):
    init = journal.iloc[0]["total"] if not journal.empty else 0
    final = journal.iloc[-1]["total"] if not journal.empty else 0
    ret = (final / init - 1) * 100 if init else 0
    n = len(trades)
    wr = (trades["pnl_pct"] > 0).mean() * 100 if n else 0
    avg_hold = trades["hold_days"].mean() if n else 0
    lines = [
        f"# 箱体突破回测 {start} ~ {end}",
        "",
        f"- setup_tag: `{SETUP_TAG}`",
        f"- 箱体: {cfg.box_min_days}~{cfg.box_max_days}日, 宽度 {cfg.box_min_width_pct*100:.0f}~{cfg.box_max_width_pct*100:.0f}%",
        f"- 触顶/触底: ≥{cfg.min_top_touches}次; 整理期缩量≤{cfg.max_box_vol_vs_pre20:.0%}",
        f"- 突破: 收盘>箱顶×{cfg.breakout_buffer}, 量/箱均≥{cfg.min_vol_vs_box_avg}",
        f"- 出场: 箱底 (X-011), MA{cfg.ma_exit} (X-012), 止损 -{cfg.stop_loss_pct*100:.0f}%",
        "",
        "## 汇总",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 期初 | {init:,.0f} |",
        f"| 期末 | {final:,.0f} |",
        f"| 收益率 | {ret:+.2f}% |",
        f"| 交易笔数 | {n} |",
        f"| 胜率 | {wr:.1f}% |",
        f"| 平均持股天 | {avg_hold:.1f} |",
        "",
    ]
    if n:
        lines.append("## 卖出原因")
        lines.append("")
        for reason, g in trades.groupby("exit_reason"):
            lines.append(f"- {reason}: {len(g)} 笔, 均盈亏 {g['pnl_pct'].mean():+.2f}%")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="箱体震荡突破")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default=None)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("scan")
    p_bt = sub.add_parser("backtest", help="pandas 回测")
    p_bt.add_argument("--start", required=True)
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--cash", type=float, default=100_000.0)
    p_bt.add_argument("--top", type=int, default=4)
    p_bt.add_argument("--stop-loss", type=float, default=0.02)
    p_bt.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    store = DataStore(Path(args.db))
    as_of = args.date or store.latest_trade_date()
    if args.cmd == "backtest":
        end = args.end or store.latest_trade_date()
        cfg = BoxConfig(signal_top=args.top, stop_loss_pct=args.stop_loss)
        run_box_backtest(
            Path(args.db),
            args.start,
            end,
            cfg=cfg,
            initial_cash=args.cash,
            save_csv=not args.no_save,
        )
    else:
        dragon = ThemeScanner(store, ThemeConfig(), symbols=store.theme_universe_symbols()).scan(
            as_of
        )
        result = scan_box_breakout(store, dragon)
        print_scan(result)


if __name__ == "__main__":
    main()
