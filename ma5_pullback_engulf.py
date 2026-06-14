"""
五日均线回踩反包（ma5_pullback_engulf）
规则：上涨趋势中回踩 MA5，当日温和放量反包前日阴线
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
SETUP_TAG = "ma5_pullback_engulf"


@dataclass
class EngulfConfig:
  ma5_touch_tolerance: float = 1.01
  min_vol_vs_prev: float = 1.2
  min_body_ratio: float = 1.02
  signal_top: int = 4
  watch_top: int = 8
  min_history: int = 60
  stop_loss_pct: float = 0.02
  ma_exit: int = 20
  # N-302b 量价序列：放量上涨 → 缩量下跌（≤2日）→ 反包
  vol_rally_vs_ma5: float = 1.2  # 放量：阳线量 ≥ 5日均量 × 此值
  vol_shrink_vs_ma5: float = 0.9  # 缩量：阴线量 < 5日均量 × 此值
  max_shrink_bear_days: int = 2
  min_shrink_bear_days: int = 1
  rally_lookback: int = 10  # 向前寻找放量上涨的最大交易日


@dataclass
class EngulfCandidate:
  symbol: str
  name: str
  close: float
  signal: str  # engulf | watch
  ma5: float
  ma10: float
  ma20: float
  ma60: float
  body_ratio: float
  vol_vs_prev: float
  engulf_type: str
  in_active_theme: bool = False
  gaps: List[str] = field(default_factory=list)


@dataclass
class EngulfScanResult:
  as_of: str
  scan_scope: str = "full_market"
  universe_size: int = 0
  theme_active: bool = False
  theme_name: str = ""
  candidates_engulf: List[EngulfCandidate] = field(default_factory=list)
  candidates_watch: List[EngulfCandidate] = field(default_factory=list)


def _active_theme_symbol_set(
  store: DataStore, scan: Optional[ScanResult], daily: pd.DataFrame
) -> set:
  if not scan or not scan.theme_active:
    return set()
  from theme_scan_utils import theme_symbols

  return set(theme_symbols(store, scan, daily))


def _engulf_type(close: float, prev_open: float, prev_high: float) -> str:
  if close > prev_high:
    return "close>昨高"
  if close > prev_open:
    return "close>昨开"
  return ""


def _vol_ma5(vol: pd.Series, idx: int) -> float:
  start = max(0, idx - 4)
  return float(vol.iloc[start : idx + 1].mean())


def _volume_rally_shrink_pattern(
  close: pd.Series,
  opn: pd.Series,
  vol: pd.Series,
  cfg: EngulfConfig,
) -> tuple[bool, List[str], int]:
  """
  N-302b：信号日前须出现「放量上涨 → 缩量下跌(1~2日)」。
  缩量下跌为紧邻反包前的连续阴线，且每日量 < 5日均量 × vol_shrink_vs_ma5。
  """
  gaps: List[str] = []
  n = len(close)
  if n < 4:
    return False, ["历史不足，无法判定量价序列"], 0

  end_idx = n - 1  # 信号日；缩量下跌不含信号日
  bear_indices: List[int] = []
  j = end_idx - 1
  while j >= 0 and len(bear_indices) < cfg.max_shrink_bear_days:
    cj, oj = float(close.iloc[j]), float(opn.iloc[j])
    vj = float(vol.iloc[j])
    if cj >= oj:
      break
    vol_ma5 = _vol_ma5(vol, j)
    if vol_ma5 <= 0 or vj >= vol_ma5 * cfg.vol_shrink_vs_ma5:
      if not bear_indices:
        gaps.append(
          f"前日非缩量下跌（量{vj:.0f} vs 5均量{vol_ma5:.0f}，须<{cfg.vol_shrink_vs_ma5:.0%}）"
        )
      break
    bear_indices.append(j)
    j -= 1

  bear_count = len(bear_indices)
  if bear_count < cfg.min_shrink_bear_days:
    if not gaps:
      gaps.append("缺少缩量下跌（反包前须 1～2 日阴线且缩量）")
    return False, gaps, bear_count
  if bear_count > cfg.max_shrink_bear_days:
    gaps.append(f"缩量下跌{bear_count}日，超过上限{cfg.max_shrink_bear_days}日")
    return False, gaps, bear_count

  first_bear = min(bear_indices)
  rally_found = False
  search_from = first_bear - 1
  search_to = max(-1, first_bear - cfg.rally_lookback - 1)
  for k in range(search_from, search_to, -1):
    ck, ok = float(close.iloc[k]), float(opn.iloc[k])
    vk = float(vol.iloc[k])
    vol_ma5 = _vol_ma5(vol, k)
    if ck > ok and vol_ma5 > 0 and vk >= vol_ma5 * cfg.vol_rally_vs_ma5:
      rally_found = True
      break

  if not rally_found:
    gaps.append(
      f"前{cfg.rally_lookback}日内无放量上涨（须阳线且量≥5均量×{cfg.vol_rally_vs_ma5:.1f}）"
    )
    return False, gaps, bear_count

  return True, gaps, bear_count


def _analyze_symbol(
  sub: pd.DataFrame, cfg: EngulfConfig, as_of: pd.Timestamp
) -> Optional[dict]:
  sub = sub.sort_values("trade_date")
  sub = sub[sub["trade_date"] <= as_of]
  if len(sub) < cfg.min_history + 1:
    return None

  close = sub["close"].astype(float)
  opn = sub["open"].astype(float)
  high = sub["high"].astype(float)
  low = sub["low"].astype(float)
  vol = sub["volume"].astype(float)

  c = float(close.iloc[-1])
  o = float(opn.iloc[-1])
  h = float(high.iloc[-1])
  l = float(low.iloc[-1])
  v = float(vol.iloc[-1])
  if c <= 0 or o <= 0:
    return None

  c1 = float(close.iloc[-2])
  o1 = float(opn.iloc[-2])
  h1 = float(high.iloc[-2])
  v1 = float(vol.iloc[-2])

  ma5 = float(close.rolling(5).mean().iloc[-1])
  ma10 = float(close.rolling(10).mean().iloc[-1])
  ma20 = float(close.rolling(20).mean().iloc[-1])
  ma60 = float(close.rolling(60).mean().iloc[-1])
  if any(pd.isna(x) for x in (ma5, ma10, ma20, ma60)):
    return None

  uptrend = ma20 > ma60 and c > ma20
  ma_bull = ma5 > ma10 > ma20
  trend_ok = uptrend and ma_bull
  pullback = l <= ma5 * cfg.ma5_touch_tolerance and c > ma5
  yday_bear = c1 < o1
  today_bull = c > o
  engulf = c > o1 or c > h1
  vol_ok = v1 > 0 and v > v1 * cfg.min_vol_vs_prev
  body_strong = c / o > cfg.min_body_ratio
  vol_vs_prev = v / v1 if v1 > 0 else 0.0
  body_ratio = c / o

  vol_pattern_ok, vol_pattern_gaps, shrink_days = _volume_rally_shrink_pattern(
    close, opn, vol, cfg
  )

  checks = {
    "trend_ok": trend_ok,
    "vol_pattern": vol_pattern_ok,
    "pullback": pullback,
    "yday_bear": yday_bear,
    "today_bull": today_bull,
    "engulf": engulf,
    "vol_ok": vol_ok,
    "body_strong": body_strong,
  }
  gaps: List[str] = []
  if not uptrend:
    gaps.append(f"非上升趋势（收{c:.2f} MA20={ma20:.2f} MA60={ma60:.2f}，须收>MA20且MA20>MA60）")
  if not ma_bull:
    gaps.append(f"非多头排列（MA5/10/20={ma5:.2f}/{ma10:.2f}/{ma20:.2f}，须MA5>MA10>MA20）")
  if not vol_pattern_ok:
    gaps.extend(vol_pattern_gaps)
  if not pullback:
    gaps.append(f"未有效回踩MA5（低{l:.2f} 收{c:.2f} MA5={ma5:.2f}）")
  if not yday_bear:
    gaps.append("前日非阴线")
  if not today_bull:
    gaps.append("当日非阳线")
  if not engulf:
    gaps.append("未反包前日实体/高点")
  if not vol_ok:
    gaps.append(f"量能不足（今/昨={vol_vs_prev:.2f}，需≥{cfg.min_vol_vs_prev:.2f}）")
  if not body_strong:
    gaps.append(f"实体偏弱（收/开={body_ratio:.3f}，需>{cfg.min_body_ratio:.2f}）")

  is_engulf = all(checks.values())
  is_watch = (
    trend_ok
    and pullback
    and yday_bear
    and today_bull
    and not is_engulf
    and sum(checks.values()) >= 6
  )
  if not is_engulf and not is_watch:
    return None

  name = str(sub["name"].iloc[-1] or sub["symbol"].iloc[-1])
  score = body_ratio * 100 + vol_vs_prev * 20 + (1 if c > h1 else 0) * 10
  return {
    "symbol": str(sub["symbol"].iloc[-1]),
    "name": name,
    "close": c,
    "signal": "engulf" if is_engulf else "watch",
    "ma5": ma5,
    "ma10": ma10,
    "ma20": ma20,
    "ma60": ma60,
    "body_ratio": body_ratio,
    "vol_vs_prev": vol_vs_prev,
    "engulf_type": _engulf_type(c, o1, h1) if engulf else "",
    "gaps": gaps,
    "score": score,
    "shrink_days": shrink_days,
  }


def _engulf_signals_from_panel(
  daily: pd.DataFrame,
  as_of: pd.Timestamp,
  cfg: EngulfConfig,
  exclude: Optional[set] = None,
  theme_syms: Optional[set] = None,
) -> List[dict]:
  """从预加载面板提取当日反包信号（回测用）"""
  exclude = exclude or set()
  theme_syms = theme_syms or set()
  panel = daily[daily["trade_date"] <= as_of]
  signals: List[dict] = []
  for sym, grp in panel.groupby("symbol"):
    if sym in exclude:
      continue
    row = _analyze_symbol(grp, cfg, as_of)
    if not row or row["signal"] != "engulf":
      continue
    row["in_active_theme"] = sym in theme_syms
    signals.append(row)
  signals.sort(key=lambda r: (r["in_active_theme"], r["score"]), reverse=True)
  return signals


def scan_ma5_pullback_engulf(
  store: DataStore,
  scan: Optional[ScanResult] = None,
  cfg: Optional[EngulfConfig] = None,
  exclude_symbols: Optional[List[str]] = None,
  daily: Optional[pd.DataFrame] = None,
  quiet: bool = False,
) -> EngulfScanResult:
  """全市场 A 股扫描；scan 仅用于标注是否属当日 Dragon 主线成分"""
  cfg = cfg or EngulfConfig()
  exclude = set(exclude_symbols or [])
  as_of = (scan.as_of if scan else None) or store.latest_trade_date()
  if not as_of:
    return EngulfScanResult(as_of="", scan_scope="full_market")

  if not quiet:
    logging.info("五日均线回踩反包：加载全市场行情 %s …", as_of)
  if daily is None:
    daily, _ = store.load_panel(as_of, lookback=90, symbols=None)
  else:
    daily = daily[daily["trade_date"] <= pd.Timestamp(as_of)].copy()
  if daily.empty:
    return EngulfScanResult(as_of=as_of, scan_scope="full_market")

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
    if row["signal"] == "engulf":
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

  def _to_c(r: dict) -> EngulfCandidate:
    return EngulfCandidate(
      symbol=r["symbol"],
      name=r["name"],
      close=round(r["close"], 2),
      signal=r["signal"],
      ma5=round(r["ma5"], 2),
      ma10=round(r["ma10"], 2),
      ma20=round(r["ma20"], 2),
      ma60=round(r["ma60"], 2),
      body_ratio=round(r["body_ratio"], 3),
      vol_vs_prev=round(r["vol_vs_prev"], 2),
      engulf_type=r.get("engulf_type") or "",
      in_active_theme=bool(r.get("in_active_theme")),
      gaps=r.get("gaps") or [],
    )

  return EngulfScanResult(
    as_of=as_of,
    scan_scope="full_market",
    universe_size=scanned,
    theme_active=bool(scan and scan.theme_active),
    theme_name=scan.theme_name if scan and scan.theme_active else "",
    candidates_engulf=[_to_c(r) for r in signals[: cfg.signal_top]],
    candidates_watch=[_to_c(r) for r in watches[: cfg.watch_top]],
  )


def print_scan(result: EngulfScanResult, cfg: Optional[EngulfConfig] = None):
  cfg = cfg or EngulfConfig()
  print("\n" + "=" * 60)
  print(f"五日均线回踩反包 {result.as_of}  setup_tag={SETUP_TAG}")
  print(f"范围: 全市场 A 股 {result.universe_size} 只")
  if result.theme_active:
    print(f"当日 Dragon 主线: {result.theme_name}（表中 ★=主线成分）")
  else:
    print("当日无 Dragon 主线")
  print("\n【反包·可买】")
  if not result.candidates_engulf:
    print("  （无）")
  else:
    for c in result.candidates_engulf:
      star = "★" if c.in_active_theme else ""
      print(
        f"  - {star}{c.symbol} {c.name} | 收{c.close:.2f} "
        f"MA5={c.ma5:.2f} 实体{c.body_ratio:.3f} 量{c.vol_vs_prev:.2f}x "
        f"{c.engulf_type}"
      )
  print("\n【回踩观察·待反包确认】")
  if not result.candidates_watch:
    print("  （无）")
  else:
    for c in result.candidates_watch[:5]:
      gap = "；".join(c.gaps[:2]) if c.gaps else "—"
      print(f"  - {c.symbol} {c.name} | 收{c.close:.2f} | 缺：{gap}")
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
  cfg: EngulfConfig,
) -> Optional[str]:
  pnl_pct = (px / entry_price - 1) * 100
  if pnl_pct <= -cfg.stop_loss_pct * 100:
    return "stop_loss"
  ma5 = _ma_at(sub, dt, 5)
  if ma5 == ma5 and px < ma5:
    return "ma5_break"
  ma20 = _ma_at(sub, dt, cfg.ma_exit)
  if ma20 == ma20 and px < ma20:
    return "ma20_break"
  return None


def run_engulf_backtest(
  db_path: Path,
  start: str,
  end: str,
  cfg: Optional[EngulfConfig] = None,
  initial_cash: float = 100_000.0,
  invest_ratio: float = 0.95,
  commission: float = 0.001,
  save_csv: bool = True,
  tag_suffix: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame]:
  """
  五日线回踩反包回测（setup_tag=ma5_pullback_engulf）
  信号日收盘 scan → 次日开盘买入；T+1 最早次日收盘可卖；
  出场：R-001 止损 / X-009 破 MA5 / X-010 破 MA20。
  """
  cfg = cfg or EngulfConfig()
  store = DataStore(db_path)
  daily_all, _ = store.load_panel(end, lookback=120, symbols=None)
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
  pending_buys: List[str] = []
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
      for sym in pending_buys:
        if sym in positions or len(positions) >= cfg.signal_top:
          continue
        px = _price_at(daily_all, sym, dt, "open")
        if px != px or px <= 0:
          px = _price_at(daily_all, sym, dt, "close")
        if px != px or px <= 0:
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

    for sym in list(positions.keys()):
      pos = positions[sym]
      if pos["entry_date"] >= dt_str:
        continue
      px = _price_at(daily_all, sym, dt, "close")
      if px != px:
        continue
      sub = daily_all[daily_all["symbol"] == sym]
      reason = _exit_reason(sub, pd.Timestamp(dt), px, pos["entry_price"], cfg)
      if reason:
        pnl_pct = (px / pos["entry_price"] - 1) * 100
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

    if dt_str not in signal_cache:
      signal_cache[dt_str] = _engulf_signals_from_panel(
        daily_all, pd.Timestamp(dt), cfg
      )
    signals = signal_cache[dt_str]
    pending_buys = [r["symbol"] for r in signals[: cfg.signal_top] if r["symbol"] not in positions]

    stock_value = sum(
      positions[s]["shares"] * _price_at(daily_all, s, dt, "close")
      for s in positions
      if _price_at(daily_all, s, dt, "close") == _price_at(daily_all, s, dt, "close")
    )
    total = cash + stock_value
    day_ret = (total / prev_total - 1) * 100 if prev_total > 0 else 0.0
    daily_rows.append(
      {
        "date": dt_str,
        "holdings": ",".join(positions.keys()) if positions else "现金",
        "n_holdings": len(positions),
        "buy_signals": ",".join(r["symbol"] for r in signals[: cfg.signal_top]),
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
  _print_engulf_backtest_report(daily, trades, start, end, cfg, initial_cash)

  if save_csv and not daily.empty:
    out_dir = ROOT / "docs/trading-system/backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{start}_{end}_engulf{tag_suffix}".replace("-", "")
    daily_path = out_dir / f"ma5_pullback_engulf_journal_{tag}.csv"
    trades_path = out_dir / f"ma5_pullback_engulf_trades_{tag}.csv"
    md_path = out_dir / f"ma5_pullback_engulf_{tag}.md"
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    if not trades.empty:
      trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    _write_engulf_backtest_md(md_path, daily, trades, start, end, cfg, initial_cash)
    print(f"\n日记已保存: {daily_path}")
    if not trades.empty:
      print(f"交易明细已保存: {trades_path}")
    print(f"报告已保存: {md_path}")

  return daily, trades


def _print_engulf_backtest_report(
  daily: pd.DataFrame,
  trades: pd.DataFrame,
  start: str,
  end: str,
  cfg: EngulfConfig,
  initial_cash: float,
):
  print("\n" + "=" * 68)
  print(f"回测 {SETUP_TAG}  {start} ~ {end}")
  print(
    f"参数: 持仓≤{cfg.signal_top} | 止损-{cfg.stop_loss_pct*100:.0f}% | "
    f"出场 X-009 MA5 / X-010 MA{cfg.ma_exit} | 期初 {initial_cash:,.0f} 元"
  )
  print("假设：信号日收盘 scan（N-301~N-303）→ 次日开盘买入；T+1；佣金 0.1% 双边")
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
  win_rate = (trades["pnl_pct"] > 0).mean() * 100 if not trades.empty else 0.0
  print(f"期末: {final:,.2f} 元 | 总收益: {total_ret:+.2f}% | 最大回撤: {abs(max_dd):.2f}%")
  print(f"平仓: {len(trades)} 笔 | 胜率: {win_rate:.1f}%")
  if not trades.empty:
    print(f"平均持股: {trades['hold_days'].mean():.1f} 日 | 盈亏比: ", end="")
    wins = trades.loc[trades["pnl_pct"] > 0, "pnl_pct"]
    losses = trades.loc[trades["pnl_pct"] <= 0, "pnl_pct"]
    if len(wins) and len(losses):
      print(f"{wins.mean() / abs(losses.mean()):.2f}")
    else:
      print("—")
    by_reason = trades.groupby("exit_reason")["pnl_pct"].agg(["count", "mean"])
    print("\n出场原因分布:")
    for reason, row in by_reason.iterrows():
      print(f"  {reason}: {int(row['count'])} 笔, 均盈亏 {row['mean']:+.2f}%")


def _write_engulf_backtest_md(
  path: Path,
  daily: pd.DataFrame,
  trades: pd.DataFrame,
  start: str,
  end: str,
  cfg: EngulfConfig,
  initial_cash: float,
):
  final = float(daily.iloc[-1]["total"]) if not daily.empty else initial_cash
  total_ret = (final / initial_cash - 1) * 100
  max_dd = 0.0
  peak = initial_cash
  for t in daily.get("total", []):
    v = float(t)
    peak = max(peak, v)
    max_dd = min(max_dd, (v / peak - 1) * 100)
  win_rate = (trades["pnl_pct"] > 0).mean() * 100 if not trades.empty else 0.0
  lines = [
    f"# 五日均线回踩反包回测 {start} ~ {end}",
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
    f"- MA5 回踩容差: {cfg.ma5_touch_tolerance}",
    f"- 量能比: ≥{cfg.min_vol_vs_prev}",
    f"- 实体比: >{cfg.min_body_ratio}",
    f"- 持仓上限: {cfg.signal_top}",
    f"- 止损: -{cfg.stop_loss_pct*100:.0f}% (R-001)",
    f"- 出场: MA5 (X-009), MA{cfg.ma_exit} (X-010)",
    "",
  ]
  if not trades.empty:
    lines.extend([
      "## 持股统计",
      f"- 平均持股: {trades['hold_days'].mean():.1f} 交易日",
      f"- 中位持股: {trades['hold_days'].median():.0f} 交易日",
      "",
      "## 出场分布",
      "",
    ])
    for reason, g in trades.groupby("exit_reason"):
      lines.append(f"- {reason}: {len(g)} 笔, 均盈亏 {g['pnl_pct'].mean():+.2f}%")
    lines.extend([
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
    "- 信号日收盘 scan（N-301~N-303）→ 次日开盘买入",
    "- 全市场 A 股扫描，不含 Dragon 主线排序（回测按 score 排序）",
  ])
  path.write_text("\n".join(lines), encoding="utf-8")


def run_engulf_day3_backtest(
  db_path: Path,
  start: str,
  end: str,
  cfg: Optional[EngulfConfig] = None,
  save_csv: bool = True,
  exit_at: str = "open",
) -> pd.DataFrame:
  """
  第三天胜率回测：信号日 T 收盘确认 → T+1 开盘买入 → T+2 评估盈亏。
  exit_at: open（第三天开盘卖，默认）| close（第三天收盘卖）
  统计全市场所有 N-303 信号（不限 top4）。
  """
  cfg = cfg or EngulfConfig()
  store = DataStore(db_path)
  import sqlite3

  with sqlite3.connect(store.db_path) as conn:
    n_range = conn.execute(
      """
      SELECT COUNT(DISTINCT trade_date) FROM stock_daily
      WHERE trade_date >= ? AND trade_date <= ?
      """,
      (start, end),
    ).fetchone()[0]
  lookback = max(120, int(n_range) + cfg.min_history + 30)
  daily_all, _ = store.load_panel(end, lookback=lookback, symbols=None)
  if daily_all.empty:
    logging.error("未加载到行情")
    return pd.DataFrame()

  name_map = (
    daily_all[["symbol", "name"]]
    .drop_duplicates("symbol")
    .set_index("symbol")["name"]
    .astype(str)
    .to_dict()
  )
  trade_dates = sorted(daily_all["trade_date"].unique())
  trade_dates = [
    pd.Timestamp(d).strftime("%Y-%m-%d")
    for d in trade_dates
    if start <= pd.Timestamp(d).strftime("%Y-%m-%d") <= end
  ]
  if len(trade_dates) < 3:
    logging.error("回测区间交易日不足")
    return pd.DataFrame()

  signal_set = set(trade_dates)
  all_dates = sorted(
    pd.Timestamp(d).strftime("%Y-%m-%d") for d in daily_all["trade_date"].unique()
  )
  date_pos = {d: i for i, d in enumerate(all_dates)}
  rows: List[dict] = []
  syms = daily_all["symbol"].nunique()
  logging.info("第三天胜率回测：按标的遍历 %s 只 × 区间 %s～%s", syms, start, end)

  for n_done, (sym, grp) in enumerate(daily_all.groupby("symbol"), 1):
    if n_done % 800 == 0:
      logging.info("  进度 %s/%s …", n_done, syms)
    grp = grp.sort_values("trade_date").reset_index(drop=True)
    if len(grp) < cfg.min_history + 3:
      continue
    for i in range(cfg.min_history, len(grp) - 2):
      signal_date = pd.Timestamp(grp.iloc[i]["trade_date"]).strftime("%Y-%m-%d")
      if signal_date not in signal_set:
        continue
      j = date_pos.get(signal_date)
      if j is None or j + 2 >= len(all_dates):
        continue
      buy_date = all_dates[j + 1]
      eval_date = all_dates[j + 2]
      row = _analyze_symbol(grp.iloc[: i + 1], cfg, pd.Timestamp(signal_date))
      if not row or row["signal"] != "engulf":
        continue
      entry = float(grp.iloc[i + 1]["open"])
      if entry <= 0:
        entry = float(grp.iloc[i + 1]["close"])
      if exit_at == "open":
        exit_px = float(grp.iloc[i + 2]["open"])
        if exit_px <= 0:
          exit_px = float(grp.iloc[i + 2]["close"])
        exit_label = "eval_open"
      else:
        exit_px = float(grp.iloc[i + 2]["close"])
        exit_label = "eval_close"
      if entry <= 0 or exit_px <= 0:
        continue
      pnl_pct = (exit_px / entry - 1) * 100
      rows.append(
        {
          "signal_date": signal_date,
          "buy_date": buy_date,
          "eval_date": eval_date,
          "symbol": sym,
          "name": name_map.get(sym, sym),
          "entry_price": round(entry, 2),
          exit_label: round(exit_px, 2),
          "exit_at": exit_at,
          "pnl_pct": round(pnl_pct, 2),
          "win": pnl_pct > 0,
        }
      )

  trades = pd.DataFrame(rows)
  exit_desc = "T+2 开盘卖" if exit_at == "open" else "T+2 收盘评"
  print("\n" + "=" * 68)
  print(f"第三天胜率回测 {SETUP_TAG}  {start} ~ {end}")
  print(f"流程：信号日 T 收盘 → T+1 开盘买 → {exit_desc}（第三天）")
  print(
    f"趋势过滤：收>MA20 且 MA20>MA60；多头排列 MA5>MA10>MA20；"
    f"放量上涨→缩量下跌(≤{cfg.max_shrink_bear_days}日)→反包（N-301/302b v3）"
  )
  print("=" * 68)
  if trades.empty:
    print("无有效样本")
    return trades

  n = len(trades)
  wins = int(trades["win"].sum())
  win_rate = wins / n * 100
  avg_pnl = float(trades["pnl_pct"].mean())
  med_pnl = float(trades["pnl_pct"].median())
  print(f"样本数: {n} 笔（信号日×标的，全市场）")
  print(f"第三天胜率: {win_rate:.1f}% ({wins}/{n})")
  print(f"第三天平均盈亏: {avg_pnl:+.2f}% | 中位数: {med_pnl:+.2f}%")

  by_month = trades.assign(month=trades["signal_date"].str[:7]).groupby("month")
  print("\n按月第三天胜率:")
  for month, g in by_month:
    wr = g["win"].mean() * 100
    print(f"  {month}: {wr:.1f}% ({int(g['win'].sum())}/{len(g)}), 均盈亏 {g['pnl_pct'].mean():+.2f}%")

  if save_csv:
    out_dir = ROOT / "docs/trading-system/backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{start}_{end}_day3_{exit_at}".replace("-", "")
    path = out_dir / f"ma5_pullback_engulf_day3_{tag}.csv"
    trades.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n明细已保存: {path}")
  print("=" * 68)
  return trades


def main():
  parser = argparse.ArgumentParser(description="五日均线回踩反包")
  parser.add_argument("--db", default=str(DEFAULT_DB))
  parser.add_argument("--date", default=None)
  sub = parser.add_subparsers(dest="cmd")
  sub.add_parser("scan")
  p_bt = sub.add_parser("backtest", help="pandas 回测")
  p_bt.add_argument("--start", required=True)
  p_bt.add_argument("--end", default=None)
  p_bt.add_argument("--cash", type=float, default=100_000.0)
  p_bt.add_argument("--top", type=int, default=4, help="最大持仓")
  p_bt.add_argument("--stop-loss", type=float, default=0.02, help="止损比例，默认 0.02 (R-001)")
  p_bt.add_argument("--no-save", action="store_true")
  p_d3 = sub.add_parser("backtest-day3", help="第三天胜率（T信号→T+1买→T+2评）")
  p_d3.add_argument("--start", required=True)
  p_d3.add_argument("--end", default=None)
  p_d3.add_argument("--exit-at", choices=("open", "close"), default="open",
                    help="第三天卖出价：open=开盘（默认）| close=收盘")
  p_d3.add_argument("--no-save", action="store_true")
  args = parser.parse_args()

  store = DataStore(Path(args.db))
  as_of = args.date or store.latest_trade_date()
  if args.cmd == "backtest":
    end = args.end or store.latest_trade_date()
    cfg = EngulfConfig(signal_top=args.top, stop_loss_pct=args.stop_loss)
    run_engulf_backtest(
      Path(args.db),
      args.start,
      end,
      cfg=cfg,
      initial_cash=args.cash,
      save_csv=not args.no_save,
    )
  elif args.cmd == "backtest-day3":
    end = args.end or store.latest_trade_date()
    run_engulf_day3_backtest(
      Path(args.db),
      args.start,
      end,
      save_csv=not args.no_save,
      exit_at=args.exit_at,
    )
  else:
    dragon = ThemeScanner(store, ThemeConfig(), symbols=store.theme_universe_symbols()).scan(
      as_of
    )
    result = scan_ma5_pullback_engulf(store, dragon)
    print_scan(result)


if __name__ == "__main__":
  main()
