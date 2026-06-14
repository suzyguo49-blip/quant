"""
科技·温和放量·相对低估（tech_gentle_value_rise）
规则：playbooks/notes.md N-201～N-204
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import baostock as bs
import numpy as np
import pandas as pd

from monthly_theme_dragon import DEFAULT_DB, DataStore, is_a_share
from providers.pe_growth import PeGrowthGate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

ROOT = Path(__file__).resolve().parent
SETUP_TAG = "tech_gentle_value_rise"

# 科技题材池（manual_themes 科技成长相关，与 Dragon 题材表对齐）
# core: T01 CPO | T02 PCB | T03 存储 | T04 光纤 | T11 半导体
# secondary/thematic: T06 航天 | T10 机器人 | T12 低空 | T13 汽智 | T14 消费电子 | T15 家电 | T18 AIPC
TECH_THEME_IDS = {
    "T01", "T02", "T03", "T04", "T06",
    "T10", "T11", "T12", "T13",
    "T14", "T15", "T18",
}

TECH_INDUSTRY_KEYWORDS = (
    "计算机",
    "电子",
    "通信",
    "半导体",
    "软件",
    "光学",
    "互联网",
    "元件",
    "设备",
    "IT",
    "科技",
    "电器",
)


@dataclass
class TechValueConfig:
    gentle_days: int = 5
    min_daily_pct: float = 0.3
    max_daily_pct: float = 5.0
    min_ret5: float = 0.02
    max_ret5: float = 0.15
    vol_end_vs_start: float = 1.05
    min_vol_floor_ratio: float = 0.80
    industry_pe_pct_max: float = 0.45
    pe_dynamic_vs_static_ratio: float = 0.98
    static_pe_lookback_days: int = 60
    room_to_high60_min: float = 0.03
    room_to_high60_max: float = 0.20
    top_picks: int = 4
    watch_top: int = 8
    max_pe_fetch: int = 120
    ma_exit: int = 60  # 已停用 X-007，保留字段供日后恢复
    use_x007: bool = False  # X-007 趋势出场（默认关闭，仅 X-008 + 止损）
    require_bottom: bool = False
    min_pullback_from_120d_high: float = 0.15
    max_ret60: float = 0.50


@dataclass
class TechValueCandidate:
    symbol: str
    name: str
    close: float
    signal: str
    ret5_pct: float
    pe_ttm: float
    pe_static_proxy: float
    pe_industry_pct: float
    vol_trend: float
    room_high60_pct: float
    chip_score: float
    industry: str
    gaps: List[str] = field(default_factory=list)


@dataclass
class TechValueScanResult:
    as_of: str
    universe_size: int = 0
    candidates_buy: List[TechValueCandidate] = field(default_factory=list)
    candidates_watch: List[TechValueCandidate] = field(default_factory=list)


def tech_universe_symbols(store: DataStore, daily: pd.DataFrame) -> Set[str]:
    syms: Set[str] = set()
    for t in store.load_manual_themes():
        if t.get("id") in TECH_THEME_IDS:
            syms.update(t.get("symbols") or [])
    ind_map = daily[["symbol", "industry"]].drop_duplicates("symbol")
    for _, row in ind_map.iterrows():
        ind = str(row.get("industry") or "")
        if any(k in ind for k in TECH_INDUSTRY_KEYWORDS):
            syms.add(row["symbol"])
    return {s for s in syms if is_a_share(s)}


def _gentle_rise_metrics(sub: pd.DataFrame, cfg: TechValueConfig) -> Optional[dict]:
    sub = sub.sort_values("trade_date").tail(cfg.gentle_days + 1)
    if len(sub) < cfg.gentle_days + 1:
        return None
    tail = sub.tail(cfg.gentle_days).copy()
    close = tail["close"].astype(float)
    vol = tail["volume"].astype(float)
    pct = tail["pct_chg"].astype(float)

    ret5 = float(close.iloc[-1] / close.iloc[0] - 1)
    if ret5 < cfg.min_ret5 or ret5 > cfg.max_ret5:
        return None

    ok_days = ((pct >= cfg.min_daily_pct) & (pct <= cfg.max_daily_pct)).sum()
    if ok_days < cfg.gentle_days - 1:
        return None

    if vol.iloc[-1] < vol.iloc[0] * cfg.vol_end_vs_start:
        return None
    if vol.min() < vol.iloc[0] * cfg.min_vol_floor_ratio:
        return None

    vol_trend = float(np.polyfit(range(cfg.gentle_days), vol.values, 1)[0])
    if vol_trend <= 0:
        return None

    return {
        "ret5": ret5,
        "vol_trend": vol_trend,
        "pct_ok": int(ok_days),
    }


def _chip_health(sub: pd.DataFrame, as_of: pd.Timestamp) -> tuple[bool, float]:
    """量价代理筹码健康：收盘偏强、上影不过长"""
    sub = sub[sub["trade_date"] <= as_of].tail(10)
    if len(sub) < 5:
        return False, 0.0
    score = 0.0
    tail5 = sub.tail(5)
    long_upper = 0
    for _, r in tail5.iterrows():
        h, l, c = float(r["high"]), float(r["low"]), float(r["close"])
        rng = h - l
        if rng <= 0:
            continue
        upper = (h - c) / rng
        if upper > 0.65:
            long_upper += 1
        body_pos = (c - l) / rng
        score += body_pos
    score /= max(len(tail5), 1)
    last = tail5.iloc[-1]
    h, l, c = float(last["high"]), float(last["low"]), float(last["close"])
    rng = h - l
    last_ok = rng > 0 and (c - l) / rng >= 0.45
    healthy = long_upper <= 1 and last_ok and score >= 0.45
    return healthy, score


def _bottom_position_ok(sub: pd.DataFrame, as_of: pd.Timestamp, cfg: TechValueConfig) -> bool:
    """底部位置：距 120 日高点回撤 ≥15%，且 60 日涨幅 <50%"""
    if not cfg.require_bottom:
        return True
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < 120:
        return False
    close = float(sub.iloc[-1]["close"])
    high120 = float(sub["close"].tail(120).max())
    if high120 <= 0:
        return False
    pullback = (high120 - close) / high120
    if pullback < cfg.min_pullback_from_120d_high:
        return False
    if len(sub) >= 60:
        base60 = float(sub.iloc[-60]["close"])
        if base60 > 0 and close / base60 - 1 >= cfg.max_ret60:
            return False
    return True


def _upstream_room(sub: pd.DataFrame, as_of: pd.Timestamp, cfg: TechValueConfig) -> Optional[float]:
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < 60:
        return None
    close = float(sub.iloc[-1]["close"])
    high60 = float(sub["close"].tail(60).max())
    if high60 <= 0:
        return None
    room = (high60 - close) / high60
    if room < cfg.room_to_high60_min or room > cfg.room_to_high60_max:
        return None
    return room


def _fetch_pe_ttm(symbol: str, date: str, lookback_start: str) -> tuple[float, float]:
    """返回 (当日 peTTM, 静态代理=lookback起点 peTTM)"""
    try:
        lg = bs.login()
        if lg.error_code != "0":
            return float("nan"), float("nan")
        rs = bs.query_history_k_data_plus(
            symbol,
            "date,peTTM",
            start_date=lookback_start,
            end_date=date,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if not rows:
            return float("nan"), float("nan")
        df = pd.DataFrame(rows, columns=["date", "peTTM"])
        df["peTTM"] = pd.to_numeric(df["peTTM"], errors="coerce")
        df = df.dropna()
        if df.empty:
            return float("nan"), float("nan")
        pe_now = float(df.iloc[-1]["peTTM"])
        pe_old = float(df.iloc[0]["peTTM"])
        return pe_now, pe_old
    except Exception:
        return float("nan"), float("nan")


def _ensure_valuation_table(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_valuation_daily (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                pe_ttm REAL,
                pb_mrq REAL,
                PRIMARY KEY (symbol, trade_date)
            )
            """
        )


def refresh_valuation_cache(
    store: DataStore, as_of: str, symbols: Optional[List[str]] = None, limit: Optional[int] = None
):
    """拉取 peTTM 写入 stock_valuation_daily（科技池）"""
    _ensure_valuation_table(store.db_path)
    daily, _ = store.load_panel(as_of, lookback=5, symbols=symbols)
    if daily.empty and symbols is None:
        daily, _ = store.load_panel(as_of, lookback=5)
    syms = list(symbols) if symbols else sorted(tech_universe_symbols(store, daily))
    if limit:
        syms = syms[:limit]
    lg = bs.login()
    if lg.error_code != "0":
        logging.error("baostock 登录失败")
        return
    try:
        with sqlite3.connect(store.db_path) as conn:
            for i, sym in enumerate(syms):
                rs = bs.query_history_k_data_plus(
                    sym,
                    "date,peTTM,pbMRQ",
                    start_date=as_of,
                    end_date=as_of,
                    frequency="d",
                    adjustflag="2",
                )
                data = rs.get_data()
                if data is not None and not data.empty:
                    pe = pd.to_numeric(data.iloc[0].get("peTTM"), errors="coerce")
                    pb = pd.to_numeric(data.iloc[0].get("pbMRQ"), errors="coerce")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO stock_valuation_daily
                        (symbol, trade_date, pe_ttm, pb_mrq) VALUES (?, ?, ?, ?)
                        """,
                        (sym, as_of, float(pe) if pd.notna(pe) else None, float(pb) if pd.notna(pb) else None),
                    )
                if (i + 1) % 50 == 0:
                    logging.info("估值缓存 %s/%s", i + 1, len(syms))
                time.sleep(0.12)
    finally:
        bs.logout()
    logging.info("估值缓存完成 %s 只 @ %s", len(syms), as_of)


def _pe_from_cache(db_path: Path, symbol: str, as_of: str, lookback_start: str) -> tuple[float, float]:
    with sqlite3.connect(db_path) as conn:
        now = conn.execute(
            "SELECT pe_ttm, pe FROM stock_daily WHERE symbol=? AND trade_date=?",
            (symbol, as_of),
        ).fetchone()
        if now and now[0] is not None and now[1] is not None:
            return float(now[0]), float(now[1])
        now = conn.execute(
            "SELECT pe_ttm FROM stock_valuation_daily WHERE symbol=? AND trade_date=?",
            (symbol, as_of),
        ).fetchone()
        old = conn.execute(
            "SELECT pe_ttm FROM stock_daily WHERE symbol=? AND trade_date<=? "
            "AND pe_ttm IS NOT NULL ORDER BY trade_date ASC LIMIT 1",
            (symbol, lookback_start),
        ).fetchone()
        if not old or old[0] is None:
            old = conn.execute(
                "SELECT pe_ttm FROM stock_valuation_daily WHERE symbol=? AND trade_date<=? "
                "ORDER BY trade_date ASC LIMIT 1",
                (symbol, lookback_start),
            ).fetchone()
    pe_now = float(now[0]) if now and now[0] is not None else float("nan")
    pe_old = float(old[0]) if old and old[0] is not None else float("nan")
    return pe_now, pe_old


def scan_tech_gentle_value(
    store: DataStore,
    as_of: Optional[str] = None,
    cfg: Optional[TechValueConfig] = None,
    exclude_symbols: Optional[List[str]] = None,
    use_cache: bool = True,
    daily: Optional[pd.DataFrame] = None,
    quiet: bool = False,
) -> TechValueScanResult:
    cfg = cfg or TechValueConfig()
    exclude = set(exclude_symbols or [])
    as_of = as_of or store.latest_trade_date()
    if not as_of:
        return TechValueScanResult(as_of="")

    _ensure_valuation_table(store.db_path)
    if not quiet:
        logging.info("科技温和放量扫描 %s …", as_of)
    if daily is None:
        daily, _ = store.load_panel(as_of, lookback=150)
    else:
        daily = daily[daily["trade_date"] <= pd.Timestamp(as_of)].copy()
    if daily.empty:
        return TechValueScanResult(as_of=as_of)

    as_of_ts = pd.Timestamp(as_of)
    tech_syms = tech_universe_symbols(store, daily)
    daily = daily[daily["symbol"].isin(tech_syms)]

    pe_gate = PeGrowthGate(store.db_path, as_of)
    lookback_start = (
        pd.Timestamp(as_of) - pd.Timedelta(days=int(cfg.static_pe_lookback_days * 1.6))
    ).strftime("%Y-%m-%d")

    phase1: List[dict] = []
    for sym, grp in daily.groupby("symbol"):
        if sym in exclude:
            continue
        gr = _gentle_rise_metrics(grp, cfg)
        if not gr:
            continue
        chip_ok, chip_score = _chip_health(grp, as_of_ts)
        if not chip_ok:
            continue
        if not _bottom_position_ok(grp, as_of_ts, cfg):
            continue
        room = _upstream_room(grp, as_of_ts, cfg)
        if room is None:
            continue
        row = grp[grp["trade_date"] <= as_of].iloc[-1]
        phase1.append(
            {
                "symbol": sym,
                "name": str(row.get("name") or sym),
                "close": float(row["close"]),
                "industry": str(grp["industry"].dropna().iloc[-1] if grp["industry"].notna().any() else ""),
                "ret5": gr["ret5"],
                "vol_trend": gr["vol_trend"],
                "room": room,
                "chip_score": chip_score,
                "score_pre": gr["ret5"] * 100 + chip_score * 10 + (1 - room) * 5,
            }
        )

    phase1.sort(key=lambda x: -x["score_pre"])
    phase1 = phase1[: cfg.max_pe_fetch]

    pe_records: List[dict] = []
    for i, r in enumerate(phase1):
        pe_ttm, pe_static = pe_gate.pe_pair(r["symbol"])
        if pe_ttm is not None and pe_static is not None:
            pe_now, pe_old = pe_ttm, pe_static
        elif use_cache:
            pe_now, pe_old = _pe_from_cache(store.db_path, r["symbol"], as_of, lookback_start)
        else:
            pe_now, pe_old = float("nan"), float("nan")
        if (pe_ttm is None or pe_static is None) and not np.isfinite(pe_now):
            pe_now, pe_old = _fetch_pe_ttm(r["symbol"], as_of, lookback_start)
            time.sleep(0.15)
        r["pe_now"] = pe_now
        r["pe_old"] = pe_old
        if np.isfinite(pe_now) and pe_now > 0:
            pe_records.append({**r, "pe_now": pe_now})

    if not pe_records:
        return TechValueScanResult(as_of=as_of, universe_size=len(tech_syms))

    df_pe = pd.DataFrame(pe_records)
    for ind, g in df_pe.groupby("industry"):
        if not ind or ind == "nan":
            continue
        valid = g["pe_now"] > 0
        if valid.sum() < 3:
            continue
        ranks = g.loc[valid, "pe_now"].rank(pct=True)
        df_pe.loc[ranks.index, "pe_ind_pct"] = ranks

    if "pe_ind_pct" not in df_pe.columns:
        df_pe["pe_ind_pct"] = df_pe["pe_now"].rank(pct=True)

    buys: List[dict] = []
    watches: List[dict] = []

    for _, r in df_pe.iterrows():
        gaps = []
        pe_now = float(r["pe_now"])
        pe_old = float(r.get("pe_old") or float("nan"))
        pe_ind = float(r.get("pe_ind_pct") or 0.5)

        if pe_gate.ok(r["symbol"]):
            pe_ok = True
        else:
            pe_ok = (
                np.isfinite(pe_now)
                and pe_now > 0
                and np.isfinite(pe_old)
                and pe_old > 0
                and pe_now < pe_old * cfg.pe_dynamic_vs_static_ratio
            )
        if not pe_ok:
            if not np.isfinite(pe_now) or pe_now <= 0:
                gaps.append("PE 数据缺失")
            elif pe_gate.pe_pair(r["symbol"])[0] is not None:
                gaps.append(f"PE(TTM){pe_now:.1f}≥PE(静){pe_old:.1f}(C-104)")
            else:
                gaps.append(f"动态PE未低于静态代理({pe_now:.1f} vs {pe_old:.1f})")

        ind_ok = pe_ind <= cfg.industry_pe_pct_max
        if not ind_ok:
            gaps.append(f"行业内PE分位偏高({pe_ind*100:.0f}%)")

        item = {
            "symbol": r["symbol"],
            "name": r["name"],
            "close": r["close"],
            "ret5": r["ret5"],
            "pe_now": pe_now,
            "pe_old": pe_old,
            "pe_ind": pe_ind,
            "vol_trend": r["vol_trend"],
            "room": r["room"],
            "chip_score": r["chip_score"],
            "industry": r["industry"],
            "gaps": gaps,
            "score": (1 - pe_ind) * 50 + r["chip_score"] * 20 + r["ret5"] * 100,
        }
        if pe_ok and ind_ok:
            item["signal"] = "buy"
            buys.append(item)
        elif len(gaps) <= 2:
            item["signal"] = "watch"
            watches.append(item)

    buys.sort(key=lambda x: -x["score"])
    watches.sort(key=lambda x: -x["score"])

    def _to_c(r: dict) -> TechValueCandidate:
        return TechValueCandidate(
            symbol=r["symbol"],
            name=r["name"],
            close=round(r["close"], 2),
            signal=r["signal"],
            ret5_pct=round(r["ret5"] * 100, 2),
            pe_ttm=round(r["pe_now"], 2) if np.isfinite(r["pe_now"]) else 0.0,
            pe_static_proxy=round(r["pe_old"], 2) if np.isfinite(r.get("pe_old")) else 0.0,
            pe_industry_pct=round(r["pe_ind"] * 100, 1),
            vol_trend=round(r["vol_trend"], 0),
            room_high60_pct=round(r["room"] * 100, 1),
            chip_score=round(r["chip_score"], 2),
            industry=str(r.get("industry") or ""),
            gaps=r.get("gaps") or [],
        )

    return TechValueScanResult(
        as_of=as_of,
        universe_size=len(tech_syms),
        candidates_buy=[_to_c(x) for x in buys[: cfg.top_picks]],
        candidates_watch=[_to_c(x) for x in watches[: cfg.watch_top]],
    )


def print_scan(result: TechValueScanResult):
    print("\n" + "=" * 60)
    print(f"科技温和放量·价值扫描 {result.as_of}  setup_tag={SETUP_TAG}")
    print(f"科技池 {result.universe_size} 只")
    print("\n【可买】")
    if not result.candidates_buy:
        print("  （无）")
    for c in result.candidates_buy:
        print(
            f"  - {c.symbol} {c.name} | 5日{c.ret5_pct:+.1f}% "
            f"PE动态{c.pe_ttm} < 静态代理{c.pe_static_proxy} | 行业PE分位{c.pe_industry_pct:.0f}% "
            f"距60日高{c.room_high60_pct:.0f}%"
        )
    print("\n【观察】")
    for c in result.candidates_watch[:5]:
        gap = "；".join(c.gaps[:2]) if c.gaps else "—"
        print(f"  - {c.symbol} {c.name} | 缺：{gap}")
    print("=" * 60)


LOT = 100


def _ma_at(sub: pd.DataFrame, dt: pd.Timestamp, period: int) -> float:
    tail = sub[sub["trade_date"] <= dt].tail(period)["close"].astype(float)
    if len(tail) < period:
        return float("nan")
    return float(tail.mean())


def _hold_trading_days(trade_dates: list, entry: str, exit: str) -> int:
    """A股 T+1：买入日不可卖；持股天数 = 买入日之后至卖出日（含）的交易日数"""
    ed, xd = pd.Timestamp(entry), pd.Timestamp(exit)
    days = [d for d in trade_dates if ed < pd.Timestamp(d) <= xd]
    return len(days) if days else 1


def prefetch_pe_range(
    store: DataStore,
    start: str,
    end: str,
    symbols: Optional[List[str]] = None,
    sleep_sec: float = 0.08,
) -> int:
    """批量拉取 peTTM 写入 stock_valuation_daily（回测前调用）"""
    _ensure_valuation_table(store.db_path)
    daily, _ = store.load_panel(end, lookback=5)
    syms = list(symbols) if symbols else sorted(tech_universe_symbols(store, daily))
    pe_start = (pd.Timestamp(start) - pd.Timedelta(days=int(60 * 1.6))).strftime("%Y-%m-%d")
    lg = bs.login()
    if lg.error_code != "0":
        logging.error("baostock 登录失败")
        return 0
    inserted = 0
    try:
        with sqlite3.connect(store.db_path) as conn:
            for i, sym in enumerate(syms):
                rs = bs.query_history_k_data_plus(
                    sym,
                    "date,peTTM,pbMRQ",
                    start_date=pe_start,
                    end_date=end,
                    frequency="d",
                    adjustflag="2",
                )
                while rs.error_code == "0" and rs.next():
                    row = rs.get_row_data()
                    if len(row) < 2:
                        continue
                    d, pe = row[0], row[1]
                    pb = row[2] if len(row) > 2 else None
                    pe_v = pd.to_numeric(pe, errors="coerce")
                    if pd.isna(pe_v) or float(pe_v) <= 0:
                        continue
                    pb_v = pd.to_numeric(pb, errors="coerce") if pb else None
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO stock_valuation_daily
                        (symbol, trade_date, pe_ttm, pb_mrq) VALUES (?, ?, ?, ?)
                        """,
                        (
                            sym,
                            d,
                            float(pe_v),
                            float(pb_v) if pb_v is not None and pd.notna(pb_v) else None,
                        ),
                    )
                    inserted += 1
                if (i + 1) % 50 == 0:
                    logging.info("PE 缓存 %s/%s", i + 1, len(syms))
                time.sleep(sleep_sec)
    finally:
        bs.logout()
    logging.info("PE 缓存完成：写入 %s 条，覆盖 %s 只", inserted, len(syms))
    return inserted


def _volume_exit_signal(sub: pd.DataFrame, as_of: pd.Timestamp) -> bool:
    """X-008：5 日量能趋势转负，或放量长上影"""
    sub = sub[sub["trade_date"] <= as_of].tail(10)
    if len(sub) < 6:
        return False
    tail5 = sub.tail(5)
    vol = tail5["volume"].astype(float)
    vol_trend = float(np.polyfit(range(5), vol.values, 1)[0])
    if vol_trend < 0:
        return True
    last = sub.iloc[-1]
    vol5_mean = float(vol.mean())
    h, l, c = float(last["high"]), float(last["low"]), float(last["close"])
    v = float(last["volume"])
    rng = h - l
    if rng > 0 and v > vol5_mean * 2.5 and (h - c) / rng > 0.65:
        return True
    return False


def _lot_shares(budget: float, price: float) -> int:
    if price <= 0 or budget <= 0:
        return 0
    return int(budget / price / LOT) * LOT


def _price_at(panel: pd.DataFrame, sym: str, dt: pd.Timestamp, col: str = "close") -> float:
    sub = panel[(panel["symbol"] == sym) & (panel["trade_date"] == dt)]
    if sub.empty:
        return float("nan")
    return float(sub.iloc[-1][col])


def run_tech_value_backtest(
    db_path: Path,
    start: str,
    end: str,
    cfg: Optional[TechValueConfig] = None,
    initial_cash: float = 100_000.0,
    invest_ratio: float = 0.95,
    commission: float = 0.001,
    stop_loss_pct: float = 0.05,
    prefetch_pe: bool = True,
    save_csv: bool = True,
    tag_suffix: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    科技温和放量回测（setup_tag=tech_gentle_value_rise）
    信号日收盘 scan → 次日开盘买入；T+1 最早次日收盘可卖；持有至止损 / X-008（无 X-007）。
    """
    cfg = cfg or TechValueConfig()
    store = DataStore(db_path)
    daily_seed, _ = store.load_panel(end, lookback=5)
    tech_syms = sorted(tech_universe_symbols(store, daily_seed))
    daily_all, _ = store.load_panel(end, lookback=250, symbols=tech_syms)
    if daily_all.empty:
        logging.error("未加载到行情")
        return pd.DataFrame(), pd.DataFrame()

    tech_syms = sorted(tech_universe_symbols(store, daily_all))
    daily_all = daily_all[daily_all["symbol"].isin(tech_syms)].copy()
    name_map = (
        daily_all[["symbol", "name"]]
        .drop_duplicates("symbol")
        .set_index("symbol")["name"]
        .astype(str)
        .to_dict()
    )
    logging.info("科技池 %s 只，预加载行情 %s 条", len(tech_syms), len(daily_all))

    if prefetch_pe:
        prefetch_pe_range(store, start, end, symbols=tech_syms)

    trade_dates = sorted(
        daily_all.loc[
            (daily_all["trade_date"] >= pd.Timestamp(start))
            & (daily_all["trade_date"] <= pd.Timestamp(end)),
            "trade_date",
        ].unique()
    )
    if len(trade_dates) == 0:
        logging.error("回测区间无交易日")
        return pd.DataFrame(), pd.DataFrame()

    cash = initial_cash
    positions: Dict[str, dict] = {}
    pending_buys: List[str] = []
    daily_rows: List[dict] = []
    trade_rows: List[dict] = []
    prev_total = initial_cash
    scan_cache: Dict[str, TechValueScanResult] = {}

    for i, dt in enumerate(trade_dates):
        dt_str = pd.Timestamp(dt).strftime("%Y-%m-%d")
        actions: List[str] = []

        # 次日开盘：执行前一日信号
        if pending_buys and i > 0:
            deploy_base = cash + sum(
                positions[s]["shares"] * _price_at(daily_all, s, dt, "close")
                for s in positions
                if np.isfinite(_price_at(daily_all, s, dt, "close"))
            )
            deploy = deploy_base * invest_ratio
            per_slot = deploy / cfg.top_picks if cfg.top_picks else deploy
            for sym in pending_buys:
                if sym in positions or len(positions) >= cfg.top_picks:
                    continue
                px = _price_at(daily_all, sym, dt, "open")
                if not np.isfinite(px) or px <= 0:
                    px = _price_at(daily_all, sym, dt, "close")
                if not np.isfinite(px) or px <= 0:
                    continue
                shares = _lot_shares(min(cash, per_slot), px)
                cost = shares * px * (1 + commission)
                if shares <= 0 or cost > cash:
                    continue
                cash -= cost
                positions[sym] = {
                    "shares": shares,
                    "entry_price": px,
                    "entry_date": dt_str,
                }
                actions.append(f"买入 {sym} {shares}股 @{px:.2f}")
        pending_buys = []

        # 收盘：止损 / 出场（A股 T+1：买入当日不可卖）
        for sym in list(positions.keys()):
            pos = positions[sym]
            if pos["entry_date"] >= dt_str:
                continue
            px = _price_at(daily_all, sym, dt, "close")
            if not np.isfinite(px):
                continue
            pnl_pct = (px / pos["entry_price"] - 1) * 100
            sub = daily_all[daily_all["symbol"] == sym]
            reason = None
            if pnl_pct <= -stop_loss_pct * 100:
                reason = "stop_loss"
            elif cfg.use_x007:
                ma_x = _ma_at(sub, dt, cfg.ma_exit)
                if np.isfinite(ma_x) and px < ma_x:
                    reason = f"ma{cfg.ma_exit}_break"
            if reason is None and _volume_exit_signal(sub, pd.Timestamp(dt)):
                reason = "volume_deterioration"
            if reason:
                proceeds = pos["shares"] * px * (1 - commission)
                cash += proceeds
                hold_days = _hold_trading_days(trade_dates, pos["entry_date"], dt_str)
                trade_rows.append(
                    {
                        "symbol": sym,
                        "name": name_map.get(sym, sym),
                        "shares": pos["shares"],
                        "setup_tag": SETUP_TAG,
                        "entry_date": pos["entry_date"],
                        "exit_date": dt_str,
                        "hold_days": hold_days,
                        "entry_price": round(pos["entry_price"], 2),
                        "exit_price": round(px, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "exit_reason": reason,
                    }
                )
                actions.append(f"卖出 {sym} ({reason})")
                positions.pop(sym)

        # 收盘 scan → 次日待买
        if dt_str not in scan_cache:
            scan_cache[dt_str] = scan_tech_gentle_value(
                store,
                dt_str,
                cfg=cfg,
                use_cache=True,
                daily=daily_all,
                quiet=True,
            )
        scan = scan_cache[dt_str]
        pending_buys = [c.symbol for c in scan.candidates_buy if c.symbol not in positions]

        stock_value = sum(
            positions[s]["shares"] * _price_at(daily_all, s, dt, "close")
            for s in positions
            if np.isfinite(_price_at(daily_all, s, dt, "close"))
        )
        total = cash + stock_value
        day_ret = (total / prev_total - 1) * 100 if prev_total > 0 else 0.0
        daily_rows.append(
            {
                "date": dt_str,
                "holdings": ",".join(positions.keys()) if positions else "现金",
                "n_holdings": len(positions),
                "buy_signals": ",".join(c.symbol for c in scan.candidates_buy),
                "cash": round(cash, 2),
                "stock_value": round(stock_value, 2),
                "total": round(total, 2),
                "day_return_pct": round(day_ret, 4),
                "cum_return_pct": round((total / initial_cash - 1) * 100, 4),
                "actions": "; ".join(actions) if actions else "—",
            }
        )
        prev_total = total

    daily = pd.DataFrame(daily_rows)
    trades = pd.DataFrame(trade_rows)
    _print_tech_backtest_report(
        daily, trades, start, end, cfg, initial_cash, stop_loss_pct
    )

    if save_csv and not daily.empty:
        out_dir = ROOT / "docs/trading-system/backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{start}_{end}_tv{tag_suffix}".replace("-", "")
        daily_path = out_dir / f"tech_gentle_value_journal_{tag}.csv"
        trades_path = out_dir / f"tech_gentle_value_trades_{tag}.csv"
        daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
        if not trades.empty:
            trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
        md_path = out_dir / f"tech_gentle_value_{tag}.md"
        _write_backtest_md(
            md_path, daily, trades, start, end, cfg, initial_cash, stop_loss_pct
        )
        print(f"\n日记已保存: {daily_path}")
        if not trades.empty:
            print(f"交易明细已保存: {trades_path}")
        print(f"报告已保存: {md_path}")

    return daily, trades


def _print_tech_backtest_report(
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    start: str,
    end: str,
    cfg: TechValueConfig,
    initial_cash: float,
    stop_loss_pct: float = 0.05,
):
    mode = "底部温和放量" if cfg.require_bottom else "温和放量"
    print("\n" + "=" * 68)
    print(f"回测 {SETUP_TAG}（{mode}）  {start} ~ {end}")
    print(
        f"参数: 5日温和涨 {cfg.min_ret5*100:.0f}~{cfg.max_ret5*100:.0f}% | "
        f"持仓≤{cfg.top_picks} | 止损-{stop_loss_pct*100:.0f}% | 期初 {initial_cash:,.0f} 元"
    )
    if cfg.use_x007:
        print(f"  X-007: 破 MA{cfg.ma_exit}")
    else:
        print("  X-007: 已关闭；出场仅 X-008 + 止损")
    if cfg.require_bottom:
        print(
            f"底部过滤: 距120日高点回撤≥{cfg.min_pullback_from_120d_high*100:.0f}% | "
            f"60日涨幅<{cfg.max_ret60*100:.0f}%"
        )
    print("假设：信号日收盘 scan → 次日开盘买入；T+1 不可当日卖；佣金 0.1% 双边")
    print("=" * 68)
    if daily.empty:
        print("无回测结果")
        return

    final = float(daily.iloc[-1]["total"])
    total_ret = (final / initial_cash - 1) * 100
    max_dd = 0.0
    peak = initial_cash
    for t in daily["total"]:
        v = float(t)
        peak = max(peak, v)
        max_dd = min(max_dd, (v / peak - 1) * 100)

    rets = daily["day_return_pct"].astype(float)
    sharpe = (rets.mean() / rets.std()) * (252 ** 0.5) if rets.std() > 0 else 0.0
    signal_days = (daily["buy_signals"].astype(str).str.len() > 0).sum()

    print(f"交易日: {len(daily)}")
    print(f"有【可买】信号日: {signal_days}")
    print(f"期末资产: {final:,.2f} 元")
    print(f"总收益率: {total_ret:.2f}%")
    print(f"最大回撤: {abs(max_dd):.2f}%")
    print(f"夏普比率(日频年化): {sharpe:.2f}")
    print(f"日均持仓数: {daily['n_holdings'].mean():.1f}")

    if not trades.empty:
        wins = (trades["pnl_pct"] > 0).sum()
        print(f"平仓笔数: {len(trades)}  胜率: {wins / len(trades) * 100:.1f}%")
        print(f"单笔均盈亏: {trades['pnl_pct'].mean():.2f}%")
        print(f"平均持股: {trades['hold_days'].mean():.1f} 交易日（中位 {trades['hold_days'].median():.0f}）")
        print("\n按卖出原因:")
        print(trades.groupby("exit_reason")["pnl_pct"].agg(["count", "mean"]).round(2).to_string())
        print("\n全部交易明细:")
        cols = [
            "symbol", "name", "entry_date", "exit_date", "hold_days",
            "entry_price", "exit_price", "pnl_pct", "exit_reason",
        ]
        print(trades[cols].to_string(index=False))
    else:
        print("平仓笔数: 0")

    print("\n最近 5 日:")
    cols = ["date", "n_holdings", "day_return_pct", "cum_return_pct", "actions"]
    print(daily[cols].tail(5).to_string(index=False))
    print("=" * 68)


def _write_backtest_md(
    path: Path,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    start: str,
    end: str,
    cfg: TechValueConfig,
    initial_cash: float,
    stop_loss_pct: float = 0.05,
):
    final = float(daily.iloc[-1]["total"]) if not daily.empty else initial_cash
    total_ret = (final / initial_cash - 1) * 100
    max_dd = 0.0
    peak = initial_cash
    for t in daily.get("total", []):
        v = float(t)
        peak = max(peak, v)
        max_dd = min(max_dd, (v / peak - 1) * 100)
    win_rate = (trades["pnl_pct"] > 0).mean() * 100 if not trades.empty else 0
    mode = "底部温和放量" if cfg.require_bottom else "温和放量"
    lines = [
        f"# 科技{mode}回测 {start} ~ {end}",
        "",
        f"- setup_tag: `{SETUP_TAG}`",
        f"- 期初: {initial_cash:,.0f} 元",
        f"- 期末: {final:,.2f} 元",
        f"- 总收益: {total_ret:.2f}%",
        f"- 最大回撤: {abs(max_dd):.2f}%",
        f"- 平仓笔数: {len(trades)}",
        f"- 胜率: {win_rate:.1f}%",
        "",
        "## 参数",
        f"- 模式: {mode}",
        f"- 5日涨幅: {cfg.min_ret5*100:.0f}%~{cfg.max_ret5*100:.0f}%",
        f"- 持仓上限: {cfg.top_picks}",
        f"- 止损: -{stop_loss_pct*100:.0f}%",
        f"- 出场: {'MA' + str(cfg.ma_exit) + ' (X-007) + ' if cfg.use_x007 else ''}量价恶化 (X-008)",
    ]
    if cfg.require_bottom:
        lines.extend([
            f"- 底部: 距120日高点回撤≥{cfg.min_pullback_from_120d_high*100:.0f}%",
            f"- 60日涨幅 < {cfg.max_ret60*100:.0f}%",
        ])
    lines.extend([
        "",
        "## 持股统计",
    ])
    if not trades.empty:
        lines.extend([
            f"- 平均持股: {trades['hold_days'].mean():.1f} 交易日",
            f"- 中位持股: {trades['hold_days'].median():.0f} 交易日",
            f"- 最短/最长: {trades['hold_days'].min():.0f} / {trades['hold_days'].max():.0f} 交易日",
            "",
            "## 交易明细",
            "",
            "| 代码 | 名称 | 买入日 | 卖出日 | 持股天数 | 买入价 | 卖出价 | 盈亏% | 卖出原因 |",
            "|------|------|--------|--------|----------|--------|--------|-------|----------|",
        ])
        for _, r in trades.iterrows():
            lines.append(
                f"| {r['symbol']} | {r['name']} | {r['entry_date']} | {r['exit_date']} | "
                f"{int(r['hold_days'])} | {r['entry_price']:.2f} | {r['exit_price']:.2f} | "
                f"{r['pnl_pct']:+.2f}% | {r['exit_reason']} |"
            )
    else:
        lines.append("- （无完整交易）")
    lines.extend([
        "",
        "## 说明",
        "- 信号日收盘 scan（N-201~N-204）→ 次日开盘买入",
        "- PE 数据来自 baostock peTTM 缓存",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="科技温和放量+相对低估扫描")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default=None, help="信号日 YYYY-MM-DD")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    p_ref = sub.add_parser("refresh-valuation", help="缓存科技池 peTTM")
    p_ref.add_argument("--limit", type=int, default=None)
    p_bt = sub.add_parser("backtest", help="pandas 回测")
    p_bt.add_argument("--start", required=True)
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--cash", type=float, default=100_000.0)
    p_bt.add_argument("--top", type=int, default=4, help="最大持仓")
    p_bt.add_argument("--stop-loss", type=float, default=0.05, help="止损比例，默认 0.05")
    p_bt.add_argument("--bottom", action="store_true", help="启用底部位置过滤（120日回撤≥15%）")
    p_bt.add_argument("--no-prefetch-pe", action="store_true", help="跳过 PE 预拉取")
    p_bt.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    store = DataStore(Path(args.db))
    as_of = args.date or store.latest_trade_date()
    if args.cmd == "refresh-valuation":
        refresh_valuation_cache(store, as_of, limit=args.limit)
    elif args.cmd == "backtest":
        end = args.end or store.latest_trade_date()
        cfg = TechValueConfig(top_picks=args.top, require_bottom=args.bottom)
        suffix = "_bottom5" if args.bottom and args.stop_loss == 0.05 else (
            f"_bottom_sl{int(args.stop_loss*100)}" if args.bottom else f"_sl{int(args.stop_loss*100)}"
        )
        run_tech_value_backtest(
            Path(args.db),
            args.start,
            end,
            cfg=cfg,
            initial_cash=args.cash,
            stop_loss_pct=args.stop_loss,
            prefetch_pe=not args.no_prefetch_pe,
            save_csv=not args.no_save,
            tag_suffix=suffix,
        )
    else:
        r = scan_tech_gentle_value(store, as_of)
        print_scan(r)


if __name__ == "__main__":
    main()
