#!/usr/bin/env python3
"""收盘后生成交易日报 → docs/trading-system/journal/YYYY-MM-DD-日报.md"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from briefing_top_picks import TopPicksResult, build_top_picks, format_top_picks_section  # noqa: E402
from briefing_execution import (  # noqa: E402
    collect_exec_buy_items,
    collect_exec_sell_items,
    format_execution_checklist_section,
)
from MomentumTrend import scan_momentum_picks  # noqa: E402
from box_breakout import BoxScanResult, scan_box_breakout  # noqa: E402
from ma5_bull_pullback_watch import Ma5WatchScanResult, scan_ma5_bull_pullback_watch  # noqa: E402
from ma5_pullback_engulf import EngulfScanResult, scan_ma5_pullback_engulf  # noqa: E402
from relative_strength_watch import RsWatchScanResult, scan_relative_strength_watch  # noqa: E402
from tech_gentle_value import TechValueScanResult, scan_tech_gentle_value  # noqa: E402
from monthly_theme_dragon import (  # noqa: E402
    DEFAULT_DB,
    DataStore,
    DragonCandidate,
    ScanResult,
    ThemeConfig,
    ThemeScanner,
    _trading_days_held,
)

TS_ROOT = ROOT / "docs/trading-system"
ACCOUNT_PATH = TS_ROOT / "account.yaml"
JOURNAL_DIR = TS_ROOT / "journal"
INVEST_RATIO = 0.95
LOT = 100


def load_account(path: Path) -> dict:
    if not path.exists():
        return {
            "total_capital": 400_000.0,
            "cash": 400_000.0,
            "daily_pnl_pct": 0.0,
            "pause_new_orders": False,
            "positions": [],
            "updated": "",
        }
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    long_term = list(data.get("long_term_symbols") or [])
    for p in data.get("positions") or []:
        if p.get("bucket") == "long_term" and p.get("symbol"):
            sym = str(p["symbol"])
            if sym not in long_term:
                long_term.append(sym)
    return {
        "total_capital": float(data.get("total_capital") or 400_000),
        "cash": float(data.get("cash") or 0),
        "daily_pnl_pct": float(data.get("daily_pnl_pct") or 0),
        "pause_new_orders": bool(data.get("pause_new_orders")),
        "positions": list(data.get("positions") or []),
        "updated": str(data.get("updated") or ""),
        "strategy_cash_reserve": float(data.get("strategy_cash_reserve") or 0),
        "long_term_symbols": long_term,
        "notes": str(data.get("notes") or "").strip(),
        "strategy_max_positions": int(data.get("strategy_max_positions") or 4),
        "allow_rotation": bool(data.get("allow_rotation", True)),
        "momentum_top_stocks": int(data.get("momentum_top_stocks") or 4),
        "tech_value_top": int(data.get("tech_value_top") or 4),
        "engulf_top": int(data.get("engulf_top") or 4),
        "box_top": int(data.get("box_top") or 4),
        "ma5_watch_top": int(data.get("ma5_watch_top") or 15),
        "rs_watch_top": int(data.get("rs_watch_top") or 15),
    }


def _is_long_term(p: dict, long_term: List[str]) -> bool:
    return p.get("bucket") == "long_term" or p.get("symbol") in long_term


def symbol_snapshot(store: DataStore, symbol: str, as_of: str, ma_exit: int) -> Optional[dict]:
    daily, _ = store.load_panel(as_of, lookback=90, symbols=[symbol])
    if daily.empty:
        return None
    sub = daily[daily["symbol"] == symbol].sort_values("trade_date")
    sub = sub[sub["trade_date"] <= as_of]
    if len(sub) < ma_exit:
        return None
    row = sub.iloc[-1]
    close = float(row["close"])
    ma_line = float(sub["close"].tail(ma_exit).mean())
    ma20 = float(sub["close"].tail(20).mean()) if len(sub) >= 20 else close
    name = row.get("name") or symbol
    return {
        "symbol": symbol,
        "name": str(name),
        "close": close,
        "ma_exit": round(ma_line, 2),
        "ma20": round(ma20, 2),
    }


def t002b_gaps(c: DragonCandidate, cfg: ThemeConfig) -> List[str]:
    gaps = []
    if c.pct_chg >= cfg.max_daily_pct_chg:
        gaps.append(f"当日涨幅 {c.pct_chg:.1f}% ≥ {cfg.max_daily_pct_chg}%（T-002b-1）")
    if c.pct_chg <= cfg.min_entry_pct_chg:
        gaps.append(f"当日跌幅 {c.pct_chg:.1f}% ≤ {cfg.min_entry_pct_chg}%（大跌日不买）")
    if c.ma_bias >= cfg.max_ma_bias:
        gaps.append(f"乖离 MA20 {c.ma_bias*100:.1f}% ≥ {cfg.max_ma_bias*100:.0f}%（T-002b-2）")
    if c.ma_bias < cfg.min_ma_bias:
        gaps.append(f"乖离 MA20 {c.ma_bias*100:.1f}% < {cfg.min_ma_bias*100:.0f}%（贴线假回调）")
    if c.ret5 >= 0 or c.ret5 > cfg.min_ret5:
        gaps.append(
            f"近5日 {c.ret5*100:+.1f}% 回调不足（须 ≤{cfg.min_ret5*100:.1f}%）"
        )
    return gaps


def lot_shares(budget: float, price: float) -> int:
    if price <= 0 or budget < price * LOT:
        return 0
    return int(budget / price / LOT) * LOT


def fmt_money(x: float) -> str:
    return f"{x:,.0f}"


def fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def _rotation_hints(
    strategy_positions: List[dict],
    store: DataStore,
    cfg: ThemeConfig,
    signal_date: str,
    dragon_buy: List[DragonCandidate],
    momentum_picks: List[dict],
    tech_buys: Optional[List] = None,
    engulf_buys: Optional[List] = None,
    box_buys: Optional[List] = None,
    sub_dragon_buy: Optional[List] = None,
) -> List[str]:
    """破 MA 的持仓 + 可换入的 dragon / momentum 信号"""
    lines: List[str] = []
    sell_syms: List[str] = []
    for p in strategy_positions:
        sym = p.get("symbol", "")
        snap = symbol_snapshot(store, sym, signal_date, cfg.ma_exit)
        if snap and snap["close"] < snap["ma_exit"]:
            sell_syms.append(sym)
            lines.append(
                f"- **先减**：{sym} {p.get('name', sym)}（破 MA{cfg.ma_exit} {snap['ma_exit']:.2f}，X-003）"
            )
    buy_dragon = [c for c in dragon_buy if c.symbol not in {p.get("symbol") for p in strategy_positions}]
    buy_mom = [m for m in momentum_picks if m["symbol"] not in {p.get("symbol") for p in strategy_positions}]
    if buy_dragon:
        lines.append(
            "- **Dragon 可换入**："
            + ", ".join(f"{c.symbol} {c.name}" for c in buy_dragon[:4])
        )
    sub_dragon_buy = sub_dragon_buy or []
    buy_sub = [c for c in sub_dragon_buy if c.symbol not in {p.get("symbol") for p in strategy_positions}]
    if buy_sub:
        lines.append(
            "- **支线 Dragon 可观察换入**（T-003b，非主线优先级）："
            + ", ".join(f"{c.symbol} {c.name}" for c in buy_sub[:3])
        )
    if buy_mom:
        lines.append(
            "- **Momentum 可换入**："
            + ", ".join(f"{m['symbol']} {m['name']}" for m in buy_mom[:4])
        )
    tech_buys = tech_buys or []
    buy_tech = [t for t in tech_buys if t.symbol not in {p.get("symbol") for p in strategy_positions}]
    if buy_tech:
        lines.append(
            "- **科技温和放量 可换入**："
            + ", ".join(f"{t.symbol} {t.name}" for t in buy_tech[:4])
        )
    engulf_buys = engulf_buys or []
    buy_engulf = [e for e in engulf_buys if e.symbol not in {p.get("symbol") for p in strategy_positions}]
    if buy_engulf:
        lines.append(
            "- **五日线反包 可换入**："
            + ", ".join(f"{e.symbol} {e.name}" for e in buy_engulf[:4])
        )
    box_buys = box_buys or []
    buy_box = [b for b in box_buys if b.symbol not in {p.get("symbol") for p in strategy_positions}]
    if buy_box:
        lines.append(
            "- **箱体突破 可换入**："
            + ", ".join(f"{b.symbol} {b.name}" for b in buy_box[:4])
        )
    if not lines and (buy_dragon or buy_mom or buy_tech or buy_engulf or buy_box):
        lines.append("- 策略仓已满；若换仓，先减最弱标的再按上表信号买入。")
    return lines


def build_briefing(
    scan: ScanResult,
    account: dict,
    cfg: ThemeConfig,
    store: DataStore,
    signal_date: str,
    momentum_picks: Optional[List[dict]] = None,
    rs_watch: Optional[RsWatchScanResult] = None,
    tech_value: Optional[TechValueScanResult] = None,
    engulf: Optional[EngulfScanResult] = None,
    box: Optional[BoxScanResult] = None,
    ma5_watch: Optional[Ma5WatchScanResult] = None,
    top_picks: Optional[TopPicksResult] = None,
) -> str:
    w = cfg.window_days
    total = float(account["total_capital"])
    cash = float(account["cash"])
    positions: List[dict] = account["positions"]
    pause = account["pause_new_orders"]
    daily_pnl = float(account["daily_pnl_pct"])

    long_term_syms = set(account.get("long_term_symbols") or [])
    cash_reserve = float(account.get("strategy_cash_reserve") or 0)
    tactical_cash = max(0.0, cash - cash_reserve)

    pos_value = 0.0
    long_term_mv = 0.0
    strategy_mv = 0.0
    holding_blocks: List[str] = []
    long_term_blocks: List[str] = []
    for p in positions:
        sym = p.get("symbol", "")
        shares = int(p.get("shares") or 0)
        if not sym or shares <= 0:
            continue
        cost = float(p.get("cost") or 0)
        is_lt = _is_long_term(p, list(long_term_syms))
        snap = symbol_snapshot(store, sym, signal_date, cfg.ma_exit)
        fallback_mv = float(p.get("market_value") or 0)
        if not snap:
            if fallback_mv > 0:
                mv = fallback_mv
                block = (
                    f"### {sym} {p.get('name', sym)}\n\n"
                    f"- **状态**：**长线配置**（行情库暂无 ETF 日线，市值按券商快照 **{fmt_money(mv)}** 元）\n"
                    f"- **账户约定**：不按龙头策略 X-003 机械卖出\n"
                )
                (long_term_blocks if is_lt else holding_blocks).append(block)
                if is_lt:
                    long_term_mv += mv
                else:
                    strategy_mv += mv
                pos_value += mv
                continue
            block = f"### {sym}\n\n行情缺失，请核对 `stock_data.db` 或填写 `market_value`。\n"
            (long_term_blocks if is_lt else holding_blocks).append(block)
            continue
        mv = shares * snap["close"]
        pos_value += mv
        if is_lt:
            long_term_mv += mv
        else:
            strategy_mv += mv
        pnl = (snap["close"] / cost - 1) * 100 if cost > 0 else 0.0
        pnl_txt = f"{pnl:+.2f}%" if cost > 0 else "—"
        stop_px = round(cost * (1 - cfg.stop_loss_pct), 2) if cost > 0 else 0.0
        below_ma = snap["close"] < snap["ma_exit"]
        name = p.get("name") or snap["name"]
        if is_lt:
            status = "**长线配置**（不按龙头策略 X-003 机械卖出）"
            extra = (
                f"- **账户约定**：`long_term_symbols` / `bucket: long_term`\n"
                f"- **参考**：收盘 {snap['close']:.3f}，MA{cfg.ma_exit} {snap['ma_exit']:.2f}"
                f"（仅观察，不作策略卖信号）\n"
            )
            if p.get("note"):
                extra += f"- **备注**：{p['note']}\n"
        else:
            status = (
                f"⚠ 已破 MA{cfg.ma_exit}，考虑 X-003 卖出"
                if below_ma
                else "持有（策略仓）"
            )
            extra = (
                f"- **止损 R-003**：浮亏 ≤ -{cfg.stop_loss_pct*100:.0f}%"
                f" → 约 **{stop_px:.2f}** 元\n"
                f"- **出场 X-003**：收盘 < **{snap['ma_exit']:.2f}** → 卖出\n"
                f"- **止盈（方案 A）**：无固定目标价；沿趋势持有直至 X-003 / X-004\n"
            )
            entry_d = p.get("entry_date") or signal_date
            if entry_d and entry_d != signal_date:
                hold_td = _trading_days_held(entry_d, signal_date, [entry_d, signal_date])
                if hold_td < cfg.min_hold_days:
                    extra += (
                        f"- **T-005**：已持 {hold_td} 交易日（未满 {cfg.min_hold_days}），"
                        f"暂不因 theme_off 卖出\n"
                    )
        block = (
            f"### {sym} {name}\n\n"
            f"| 股数 | 成本 | 收盘 | 浮盈 | MA{cfg.ma_exit} |\n"
            f"|------|------|------|------|------|\n"
            f"| {shares} | {cost:.2f} | {snap['close']:.2f} | {pnl_txt} | {snap['ma_exit']:.2f} |\n\n"
            f"- **状态**：{status}\n"
            f"{extra}"
        )
        (long_term_blocks if is_lt else holding_blocks).append(block)

    equity = cash + pos_value
    strategy_positions = [
        p
        for p in positions
        if int(p.get("shares") or 0) > 0 and not _is_long_term(p, list(long_term_syms))
    ]
    max_strategy = int(account.get("strategy_max_positions") or cfg.max_dragons)
    allow_rotation = bool(account.get("allow_rotation", True))
    momentum_picks = momentum_picks or []
    rs_watch = rs_watch or RsWatchScanResult(as_of=signal_date, scan_scope="full_market")
    tech_value = tech_value or TechValueScanResult(as_of=signal_date)
    engulf = engulf or EngulfScanResult(as_of=signal_date)
    box = box or BoxScanResult(as_of=signal_date)
    ma5_watch = ma5_watch or Ma5WatchScanResult(as_of=signal_date)
    tech_top = int(account.get("tech_value_top") or 4)
    engulf_top = int(account.get("engulf_top") or 4)
    box_top = int(account.get("box_top") or 4)
    ma5_watch_top = int(account.get("ma5_watch_top") or 15)
    rs_watch_top = int(account.get("rs_watch_top") or 15)

    n_strategy = len(strategy_positions)
    n_pos = len([p for p in positions if int(p.get("shares") or 0) > 0])
    slots = max(0, max_strategy - n_strategy)
    strategy_base = max(0.0, total - long_term_mv - cash_reserve)
    deploy_cap = strategy_base * INVEST_RATIO
    per_slot = deploy_cap / max_strategy if max_strategy else deploy_cap

    c003_block = daily_pnl <= -3.0
    can_open = (
        scan.theme_active
        and scan.candidates_buy
        and slots > 0
        and not pause
        and not c003_block
    )

    lines: List[str] = []
    lines.append(f"# 交易日报 {signal_date}")
    lines.append("")
    lines.append(f"> 信号日收盘判定 → **次日**执行。生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    if top_picks:
        lines.extend(format_top_picks_section(top_picks))

    # 账户
    lines.append("## 一、账户与纪律")
    lines.append("")
    acct_note = f"（快照日期：{account.get('updated') or '未填'}）"
    lines.append(f"| 项目 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总资产 | {fmt_money(total)} 元 {acct_note} |")
    lines.append(f"| 现金 | {fmt_money(cash)} 元 |")
    lines.append(f"| 持仓市值 | {fmt_money(pos_value)} 元 |")
    lines.append(f"| 合计 | {fmt_money(equity)} 元 |")
    lines.append(f"| 当日盈亏 | {daily_pnl:+.2f}% |")
    lines.append(f"| 持仓只数（全部） | {n_pos} |")
    lines.append(
        f"| 策略仓只数 | {n_strategy} / {max_strategy}（不含长线） |"
    )
    lines.append(
        f"| 换仓 | {'允许' if allow_rotation else '关闭'} |"
    )
    lines.append(f"| 可开新仓槽位 | {slots} |")
    lines.append(f"| 长线市值 | {fmt_money(long_term_mv)} 元 |")
    lines.append(f"| 策略仓市值 | {fmt_money(strategy_mv)} 元 |")
    lines.append(f"| 战术现金底仓 | {fmt_money(cash_reserve)} 元（预留） |")
    lines.append(f"| 可用于新开仓现金 | {fmt_money(tactical_cash)} 元 |")
    lines.append(
        f"| 策略资金池 | 总资产 − 长线 − 底仓 ≈ {fmt_money(strategy_base)} 元 |"
    )
    lines.append(
        f"| 策略 deploy 上限 | 约 {INVEST_RATIO*100:.0f}% → {fmt_money(deploy_cap)} 元 |"
    )
    lines.append(f"| 单槽计划资金 | 约 {fmt_money(per_slot)} 元/只 |")
    if account.get("notes"):
        lines.append("")
        lines.append(f"*说明：{account['notes'].replace(chr(10), ' ')}*")
    if pause:
        lines.append("")
        lines.append("⚠ **pause_new_orders=true**：人工暂停新开仓。")
    if c003_block:
        lines.append("")
        lines.append("⚠ **C-003**：当日亏损已达 3%，明日停止开新仓。")
    lines.append("")
    lines.append(
        "*账户更新：每日将券商截图发给 Cursor，说明「请更新 account.yaml」后重新生成本日报。*"
    )
    lines.append("")

    # Dragon 主线
    lines.append("## 二、Dragon 主线（monthly_theme_dragon）")
    lines.append("")
    if not scan.theme_active:
        lines.append("- **结论**：无合格主线 → **空仓**（T-004）")
        lines.append("- **逻辑**：无板块满足 5 日强确认（上涨日≥4、5日涨幅≥4%、成交额放大）")
    else:
        lines.append(f"- **主线**：{scan.theme_name}（{scan.theme_type}）")
        lines.append(
            f"- **强度**：{w}日涨幅 {scan.sector_return*100:.2f}% | "
            f"上涨日 {scan.up_days}/{w}"
        )
        if scan.theme_type == "manual":
            lines.append(
                "- **逻辑**：T-001 5日强确认成立；T-003 在人工题材池中择强"
            )
            industry_refs = sorted(
                (c for c in (scan.all_active_themes or []) if c.get("type") == "industry"),
                key=lambda x: -x["sector_return"],
            )[:2]
            if industry_refs:
                refs = "；".join(
                    f"{c['name']} {c['sector_return']*100:.1f}%" for c in industry_refs
                )
                lines.append(f"- **行业参考**（不参与交易）：{refs}")
        else:
            lines.append("- **逻辑**：T-001 5日强确认成立；无人工题材激活，退而选最强行业")
        if scan.sub_theme_active:
            lines.append("")
            lines.append(f"- **支线（T-003b）**：{scan.sub_theme_name}（{scan.sub_theme_type}）")
            lines.append(
                f"  - **强度**：{w}日涨幅 {scan.sub_sector_return*100:.2f}% | "
                f"上涨日 {scan.sub_up_days}/{w}"
                + ("" if scan.sub_theme_t001_ok else "（弱支线·未达 T-001）")
            )
            lines.append(
                "  - **定位**：人工题材池内次强；**观察/换仓参考**，不占主线 Dragon 开仓优先级"
            )
            if scan.sub_candidates_buy:
                sub_rows = []
                for c in scan.sub_candidates_buy:
                    tag = "T-002b 回调" if c.signal == "buy" else "T-002c 建仓"
                    sub_rows.append(f"{c.symbol} {c.name}（{tag}）")
                lines.append(f"  - **支线可买**：{'；'.join(sub_rows)}")
            else:
                lines.append("  - **支线可买**：（无，龙头池当日均大涨/大跌）")
            if scan.sub_candidates_watch:
                watch_rows = [
                    f"{c.symbol} {c.name}" for c in scan.sub_candidates_watch[:3]
                ]
                lines.append(f"  - **支线观察**：{'；'.join(watch_rows)}")
    lines.append("")

    # Dragon 明日买入
    lines.append("## 三、Dragon 明日买入（T-002b / T-002c）")
    lines.append("")
    if not scan.theme_active:
        lines.append("**不买入** — 无主线。")
    elif pause or c003_block:
        lines.append("**不买入** — 纪律限制（见第一节）。")
    elif not scan.candidates_buy:
        lines.append(
            "**不买入** — 有主线但龙头池当日均大涨/大跌，T-002b/T-002c 均无法建仓。"
        )
    elif slots <= 0:
        sigs = ", ".join(
            f"{c.symbol} {c.name}" for c in scan.candidates_buy[:max_strategy]
        )
        rot = (
            "见 **第十一节 换仓**。"
            if allow_rotation
            else "若换仓请先减策略仓（长线 **513010** 不动）。"
        )
        lines.append(
            f"**不新开仓** — 策略仓已满（{n_strategy}/{max_strategy} 只）。"
            f"Dragon 信号：**{sigs}**；{rot}"
        )
    else:
        lines.append("| 代码 | 名称 | 收盘价 | 建议股数 | 约金额 | 止损 | 出场（方案A） | 逻辑 |")
        lines.append("|------|------|--------|----------|--------|------|----------------|------|")
        for c in scan.candidates_buy[:max_strategy]:
            if slots <= 0:
                break
            snap = symbol_snapshot(store, c.symbol, signal_date, cfg.ma_exit)
            close = snap["close"] if snap else 0.0
            ma_x = snap["ma_exit"] if snap else 0.0
            budget = min(tactical_cash, per_slot)
            shares = lot_shares(budget, close) if close > 0 else 0
            amount = shares * close if shares else 0
            stop_px = round(close * (1 - cfg.stop_loss_pct), 2) if close else 0
            tag = "T-002b 回调" if c.signal == "buy" else "T-002c 建仓"
            lines.append(
                f"| {c.symbol} | {c.name} | {close:.2f} | {shares} | "
                f"{fmt_money(amount)} | R-003: ≤{stop_px}(-{cfg.stop_loss_pct*100:.0f}%) 或 MA{cfg.ma_exit} {ma_x:.2f} | "
                f"持满{cfg.min_hold_days}日后才 theme_off 卖 | {tag} |"
            )
            slots -= 1
        lines.append("")
        lines.append(
            f"- **仓位逻辑**：策略池 {fmt_money(strategy_base)} × {INVEST_RATIO*100:.0f}% "
            f"÷ 最多 {max_strategy} 只 ≈ 每只 {fmt_money(per_slot)}；"
            f"新开仓仅用超出底仓的现金（{fmt_money(tactical_cash)}），100 股整手。"
        )
        lines.append("- **执行**：见 **第十节 E-009**（10:30 后限价买；禁止开盘追买）。")
        lines.append(
            f"- **止盈**：无固定目标价；持有至破 **{cfg.ma_exit} 日线（X-003）** 或主线衰竭（X-004）。"
        )

    lines.append("")

    # Momentum
    mom_top = int(account.get("momentum_top_stocks") or 4)
    lines.append("## 四、Momentum 动量（momentum_trend_5 / E-001）")
    lines.append("")
    lines.append(
        f"- **规则**：5 日动量前 **{mom_top}** + 收盘 > MA20；排除 ST、**C-104 PE(TTM)&lt;PE(静)**、**不含恒生科技 ETF（513010）**"
    )
    if not momentum_picks:
        lines.append("- **结论**：无合格标的（数据不足或全市场无满足条件）")
    else:
        lines.append("")
        lines.append("| 排名 | 代码 | 名称 | 收盘 | MA20 | 5日动量 | 备注 |")
        lines.append("|------|------|------|------|------|---------|------|")
        held = {p.get("symbol") for p in strategy_positions}
        for m in momentum_picks:
            in_port = m["symbol"] in held
            note = "已持仓" if in_port else "可纳入动量仓"
            lines.append(
                f"| {m['rank']} | {m['symbol']} | {m['name']} | {m['close']:.2f} | "
                f"{m['ma20']:.2f} | {m['mom5_pct']:+.2f}% | {note} |"
            )
        lines.append("")
        lines.append(
            f"- **执行**：与 Dragon 共用 **{max_strategy}** 只策略槽位；"
            "跌出动量前 4 或破 MA20 则卖出（exit X-001/X-002 逻辑）。"
        )
        lines.append(
            "- **换仓**：允许时，可先减破线仓，再从本表未持仓标的中择优换入。"
        )
    lines.append("")

    # 相对抗跌观察（大盘下跌期）
    rw = rs_watch
    lines.append("## 五、相对抗跌观察（relative_strength_watch / E-008）")
    lines.append("")
    lines.append(
        f"- **扫描范围**：**全市场 A 股** {rw.universe_size} 只"
        "（剔除 ST、指数；须 **C-104**）"
    )
    lines.append(
        f"- **市场 5 日**：**{rw.index_ret5_pct:+.2f}%**（全 A 等权复合；"
        f"≤ −2% 时激活本节，规则 N-501～N-503）"
    )
    if rw.theme_active:
        lines.append(
            f"- **当日 Dragon 主线**：{rw.theme_name}（表中 **★** = 属主线成分，排序优先）"
        )
    else:
        lines.append("- **当日 Dragon 主线**：（无）")
    if not rw.regime_active:
        lines.append(
            "- **状态**：**休眠** — 大盘未进入下跌观察期，暂不输出抗跌池；"
            "可继续参考 Dragon 回调（T-002b）与 MA5 回踩观察（E-007）。"
        )
    elif rw.candidates:
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | 5日% | 超额% | MA5 | MA20 | 备注 |")
        lines.append("|------|------|------|------|-------|-----|------|------|")
        for c in rw.candidates[:rs_watch_top]:
            star = "★ " if c.in_active_theme else ""
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {c.ret5_pct:+.2f} | "
                f"{c.rs_spread_pct:+.2f} | {c.ma5:.2f} | {c.ma20:.2f} | 【观察】N-503 |"
            )
        lines.append("")
        lines.append(
            "- **定位**：**仅观察/自选**；大盘下跌期筛「跑赢市场 + 趋势未坏」标的，"
            "不占用策略槽；指数企稳 2 日后优先 Dragon T-002b 回调"
        )
    else:
        lines.append(
            "- **观察池**：（无）— 下跌观察期内无同时满足抗跌 + 多头 + C-104 的标的"
        )
    lines.append("")

    # 科技温和放量
    tv = tech_value
    lines.append("## 六、科技温和放量·相对低估（tech_gentle_value_rise / E-004）")
    lines.append("")
    lines.append(
        f"- **扫描范围**：科技池约 **{tv.universe_size}** 只"
        "（题材 T01～T04、T06、T10～T15 + 科技相关行业；排除 `long_term_symbols`）"
    )
    lines.append(
        "- **规则**：5 日温和放量上涨；**C-104 PE(TTM)&lt;PE(静)**；行业内 PE 分位 ≤45%；"
        "筹码/上方压力用量价代理（N-201～N-203）"
    )
    if tv.candidates_buy:
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | 5日% | PE动态 | PE静态代理 | 行业分位% | 距60日高% | 备注 |")
        lines.append("|------|------|------|------|--------|------------|-----------|-----------|------|")
        held = {p.get("symbol") for p in strategy_positions}
        for c in tv.candidates_buy[:tech_top]:
            note = "已持仓" if c.symbol in held else "【可买】N-204"
            lines.append(
                f"| {c.symbol} | {c.name} | {c.close:.2f} | {c.ret5_pct:+.2f} | "
                f"{c.pe_ttm:.1f} | {c.pe_static_proxy:.1f} | {c.pe_industry_pct:.0f} | "
                f"{c.room_high60_pct:.0f} | {note} |"
            )
        lines.append("")
        lines.append(
            "- **执行**：收盘确认 → 次日 **10:30 后**买入（E-009）；止损 -5%（R-003 对齐）；**仅**量价恶化（X-008）出场（X-007 已停用）"
        )
        lines.append(
            "- **PE 数据**：优先读库 `stock_valuation_daily`；缺失时现场拉 baostock（可先 "
            "`python tech_gentle_value.py refresh-valuation` 预热）"
        )
    else:
        lines.append("- **可买**：（无）— 当日无 N-204 确认标的")
    if tv.candidates_watch:
        lines.append("")
        lines.append("**观察（缺 1～2 项条件）**：")
        lines.append("")
        lines.append("| 代码 | 名称 | 5日% | 还差什么 |")
        lines.append("|------|------|------|----------|")
        for c in tv.candidates_watch[:6]:
            gap = "；".join(c.gaps[:2]) if c.gaps else "—"
            lines.append(
                f"| {c.symbol} | {c.name} | {c.ret5_pct:+.2f} | {gap} |"
            )
    lines.append("")

    # 五日均线回踩反包
    eng = engulf
    lines.append("## 七、五日均线回踩反包（ma5_pullback_engulf / E-005）")
    lines.append("")
    lines.append(
        f"- **扫描范围**：**全市场 A 股** {eng.universe_size} 只"
        "（剔除 ST；排除 `long_term_symbols`）"
    )
    lines.append(
        "- **规则**：上升趋势 + 多头排列 + **放量上涨→缩量下跌(≤2日)→反包** + **C-104**"
        " + 回踩 MA5 + 温和放量（N-301～N-303 / N-302b）"
    )
    if eng.theme_active:
        lines.append(
            f"- **当日 Dragon 主线**：{eng.theme_name}（表中 **★** = 属主线成分，排序优先）"
        )
    else:
        lines.append("- **当日 Dragon 主线**：（无）")
    if eng.candidates_engulf:
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | MA5 | 实体比 | 量/昨 | 反包 | 备注 |")
        lines.append("|------|------|------|-----|--------|-------|------|------|")
        held = {p.get("symbol") for p in strategy_positions}
        for c in eng.candidates_engulf[:engulf_top]:
            star = "★ " if c.in_active_theme else ""
            note = "已持仓" if c.symbol in held else "【反包·可买】N-303"
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {c.ma5:.2f} | "
                f"{c.body_ratio:.3f} | {c.vol_vs_prev:.2f}x | {c.engulf_type} | {note} |"
            )
        lines.append("")
        lines.append(
            "- **执行**：收盘确认 → 次日 **10:30 后**买入（E-009）；止损 R-001（−2%）；破 MA5 卖出（X-009）"
        )
    else:
        lines.append("- **反包·可买**：（无）— 全市场当日无 N-303 确认标的")
    if eng.candidates_watch:
        lines.append("")
        lines.append("**回踩观察（待反包确认）**：")
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | 还差什么 |")
        lines.append("|------|------|------|----------|")
        for c in eng.candidates_watch[:6]:
            star = "★ " if c.in_active_theme else ""
            gap = "；".join(c.gaps[:2]) if c.gaps else "—"
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {gap} |"
            )
    lines.append("")

    # 箱体震荡突破
    bx = box
    lines.append("## 八、箱体震荡突破（box_range_breakout / E-006）")
    lines.append("")
    lines.append(
        f"- **扫描范围**：**全市场 A 股** {bx.universe_size} 只"
        "（剔除 ST；排除 `long_term_symbols`）"
    )
    lines.append(
        "- **规则**：10～60 日箱体（触顶/底各≥2 次 + 缩量整理）+ **收盘**突破箱顶 + 相对整理期放量 + **C-104**"
        "（N-401～N-402）"
    )
    if bx.theme_active:
        lines.append(
            f"- **当日 Dragon 主线**：{bx.theme_name}（表中 **★** = 属主线成分，排序优先）"
        )
    else:
        lines.append("- **当日 Dragon 主线**：（无）")
    if bx.candidates_breakout:
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | 箱顶 | 箱底 | 整理日 | 触顶/底 | 量/箱均 | 备注 |")
        lines.append("|------|------|------|------|------|--------|---------|---------|------|")
        held = {p.get("symbol") for p in strategy_positions}
        for c in bx.candidates_breakout[:box_top]:
            star = "★ " if c.in_active_theme else ""
            note = "已持仓" if c.symbol in held else "【突破·可买】N-402"
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {c.box_high:.2f} | "
                f"{c.box_low:.2f} | {c.box_days} | {c.top_touches}/{c.bottom_touches} | "
                f"{c.vol_vs_box_avg:.2f}x | {note} |"
            )
        lines.append("")
        lines.append(
            "- **执行**：收盘确认 → 次日 **10:30 后**买入（E-009）；止损 R-001（−2%）；破箱底（X-011）或破 MA20（X-012）"
        )
    else:
        lines.append("- **突破·可买**：（无）— 全市场当日无 N-402 确认标的")
    if bx.candidates_watch:
        lines.append("")
        lines.append("**箱体观察（近上沿待突破）**：")
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | 箱顶 | 整理日 | 还差什么 |")
        lines.append("|------|------|------|------|--------|----------|")
        for c in bx.candidates_watch[:6]:
            star = "★ " if c.in_active_theme else ""
            gap = "；".join(c.gaps[:2]) if c.gaps else "—"
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {c.box_high:.2f} | "
                f"{c.box_days} | {gap} |"
            )
    lines.append("")

    # MA5 多头回踩观察
    mw = ma5_watch
    lines.append("## 九、MA5 多头回踩观察（ma5_bull_pullback_watch / E-007）")
    lines.append("")
    lines.append(
        f"- **扫描范围**：**全市场 A 股** {mw.universe_size} 只"
        "（剔除 ST；排除 `long_term_symbols`）"
    )
    lines.append(
        "- **规则（仅观察）**：**MA5>MA10>MA20** 多头 + **收>MA20 且 MA20>MA60** 上涨"
        " + **5日涨幅>0** + 低触 MA5、收守 MA5 + **近20日 low 不破 MA5**（N-305）"
    )
    lines.append("- **定位**：自选跟踪；**非可买**；若后续出现反包可对照第七节 E-005")
    if mw.theme_active:
        lines.append(
            f"- **当日 Dragon 主线**：{mw.theme_name}（表中 **★** = 属主线成分，排序优先）"
        )
    else:
        lines.append("- **当日 Dragon 主线**：（无）")
    if mw.candidates:
        lines.append("")
        lines.append("| 代码 | 名称 | 收盘 | MA5 | MA10 | MA20 | 5日% | 距MA5% | 备注 |")
        lines.append("|------|------|------|-----|------|------|------|--------|------|")
        for c in mw.candidates[:ma5_watch_top]:
            star = "★ " if c.in_active_theme else ""
            lines.append(
                f"| {star}{c.symbol} | {c.name} | {c.close:.2f} | {c.ma5:.2f} | "
                f"{c.ma10:.2f} | {c.ma20:.2f} | {c.ret5_pct:+.2f} | "
                f"{c.dist_ma5_pct:+.2f} | 【观察】N-305 |"
            )
    else:
        lines.append("- **观察池**：（无）— 当日无同时满足多头/上涨/回踩 MA5 的标的")
    lines.append("")

    sell_exec = collect_exec_sell_items(strategy_positions, store, cfg, signal_date)
    buy_exec = collect_exec_buy_items(
        scan=scan,
        store=store,
        cfg=cfg,
        signal_date=signal_date,
        strategy_positions=strategy_positions,
        momentum_picks=momentum_picks,
        tech_buys=tv.candidates_buy,
        engulf_buys=eng.candidates_engulf,
        box_buys=bx.candidates_breakout,
        slots=slots,
    )
    lines.extend(
        format_execution_checklist_section(
            sell_items=sell_exec,
            buy_items=buy_exec,
            pause=pause,
            c003_block=c003_block,
            slots=slots,
            allow_rotation=allow_rotation,
        )
    )

    # 换仓
    lines.append("## 十一、换仓建议（策略仓）")
    lines.append("")
    if not allow_rotation:
        lines.append("账户 `allow_rotation: false`，默认持有至出场信号。")
    else:
        rot_lines = _rotation_hints(
            strategy_positions, store, cfg, signal_date,
            scan.candidates_buy, momentum_picks,
            tv.candidates_buy,
            eng.candidates_engulf,
            bx.candidates_breakout,
            scan.sub_candidates_buy,
        )
        if rot_lines:
            lines.extend(rot_lines)
        elif slots > 0:
            lines.append(
                f"- 尚有 **{slots}** 个空槽，可按 Dragon / Momentum / 科技 / 反包 / 箱体五路信号开仓。"
            )
        else:
            lines.append("- 暂无强制换仓信号；持仓未破 MA20 时可继续持有。")
    lines.append("")

    # 自选
    lines.append("## 十二、建议加入自选（Dragon 观察池 T-002a）")
    lines.append("")
    if not scan.theme_active:
        lines.append("无主线激活，无需新增题材内自选。")
    elif not scan.candidates_watch:
        lines.append("（观察池为空）")
    else:
        lines.append("| 代码 | 名称 | 缺什么才【可买】 | 背后逻辑 |")
        lines.append("|------|------|------------------|----------|")
        for c in scan.candidates_watch[: cfg.pool_watch_size]:
            gaps = t002b_gaps(c, cfg)
            gap_txt = "；".join(gaps) if gaps else "已满足 T-002b（若未在可买表请复查）"
            logic = (
                f"T-002a 龙头池（强度排名）；"
                f"5日{c.ret5*100:+.1f}% 当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
            )
            lines.append(f"| {c.symbol} | {c.name} | {gap_txt} | {logic} |")
        lines.append("")
        lines.append("- **动作**：加入自选，**不追高**；满足 T-002b 后次日日报会出现【可买】。")

    lines.append("")

    # 持仓
    lines.append("## 十三、当前持仓处理")
    lines.append("")
    if long_term_blocks:
        lines.append(f"### 长线仓（不纳入策略 {max_strategy} 只上限）")
        lines.append("")
        lines.extend(long_term_blocks)
        lines.append("")
    if not holding_blocks:
        lines.append("无策略仓。" if long_term_blocks else "无持仓。")
    else:
        if long_term_blocks:
            lines.append("### 策略仓（按 playbook 管理）")
            lines.append("")
        lines.extend(holding_blocks)

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "**规则索引**：Dragon T-001/T-002 | Momentum E-001 | **执行 E-009** | **抗跌观察 E-008/N-501** | 科技 E-004 | "
        "反包 E-005 | 箱体 E-006 | **MA5观察 E-007/N-305** | **C-104** | T-005 | R-001/R-003 | X-003/X-009/X-011"
    )
    lines.append("")
    lines.append(
        "*由 `python scripts/trading/daily_briefing.py` 生成；"
        "信号源 Dragon + Momentum + 抗跌观察 + 科技 + 反包 + 箱体 + MA5回踩观察。*"
    )

    return "\n".join(lines)


def _remove_section(body: str, header: str) -> str:
    import re

    pat = re.compile(
        rf"^{re.escape(header)}[\s\S]*?(?=^## |\n---\n|\Z)",
        re.MULTILINE,
    )
    m = pat.search(body)
    if not m:
        return body
    return body[: m.start()] + body[m.end() :].lstrip("\n")


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s.replace("|", "").replace(":", "").replace("-", "")) == set()


def _drop_watch_tables(text: str) -> str:
    """去掉「还差什么」类观察表。"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and "还差什么" in line:
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip()


def _replace_tables_with_names(text: str, *, buy_only: bool = False) -> str:
    """将 markdown 表格替换为个股名称列表。"""
    buy_tags = ("可买", "突破", "反包", "N-204", "N-303", "N-402")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            line.strip().startswith("|")
            and i + 1 < len(lines)
            and _is_table_sep(lines[i + 1])
        ):
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            name_idx = next(
                (idx for idx, h in enumerate(header_cells) if h == "名称"),
                None,
            )
            i += 2
            names: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i]
                if buy_only and not any(tag in row for tag in buy_tags):
                    i += 1
                    continue
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                if name_idx is not None and name_idx < len(cells):
                    name = cells[name_idx]
                    if name and name not in names:
                        names.append(name)
                i += 1
            if names:
                out.append("- " + "、".join(names))
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip()


def _compact_section_content(title: str, content: str) -> str:
    if title.startswith("二、Dragon") or title.startswith("三、Dragon"):
        kept: list[str] = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith("|"):
                continue
            if s.startswith("- **支线") or s.startswith("  - **"):
                continue
            if s.startswith("**") and ("观察" in s or "粘合" in s):
                continue
            kept.append(line.rstrip())
        return "\n".join(kept).strip()

    if "相对抗跌观察" in title:
        if "休眠" in content:
            return "- 大盘未进入下跌观察期，本节跳过"

    if "MA5 多头回踩观察" in title:
        if "观察池**：（无）" in content or "无观察标的" in content:
            return "- 无观察标的"

    if any(k in title for k in ("反包", "箱体", "科技", "花开")):
        if "可买**：（无）" in content or "可**：（无）" in content:
            return "- （无）"
        content = _drop_watch_tables(content)

    buy_only = any(k in title for k in ("反包", "箱体", "科技", "花开"))
    names_text = _replace_tables_with_names(content, buy_only=buy_only)
    name_lines = [
        ln
        for ln in names_text.splitlines()
        if ln.strip().startswith("- ") and "**" not in ln and not ln.strip().startswith("- **#")
    ]

    if title.startswith("零、综合优选"):
        return name_lines[0] if name_lines else "- （无）"

    if name_lines:
        return "\n".join(name_lines)

    # 无表格时保留一行结论文本（如 Dragon 三）
    kept: list[str] = []
    skip_prefixes = (
        "- **扫描范围**",
        "- **规则**",
        "- **执行**",
        "- **换仓**",
        "- **定位**",
        "- **PE 数据**",
        "- **方法**",
        "- **新闻源**",
        "- **当日 Dragon 主线**",
        "- **市场 5 日**",
    )
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("|"):
            continue
        if any(s.startswith(p) for p in skip_prefixes):
            continue
        if s.startswith("- **#"):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip() or "- （无）"


def compact_public_briefing(body: str) -> str:
    """推送/分享用精简版：去掉换仓与自选，压缩观察池与规则说明。"""
    import re

    for hdr in ("## 十、次日执行清单", "## 十一、换仓建议", "## 十二、建议加入自选"):
        body = _remove_section(body, hdr)

    body = re.sub(r"\n---\n[\s\S]*?\*由交易系统[\s\S]*$", "\n", body)
    body = re.sub(
        r"\*由交易系统自动生成[\s\S]*$",
        "\n*公开精简版：完整信号列表，不含账户与换仓。*",
        body,
    )

    pat = re.compile(r"^(## .+)$", re.MULTILINE)
    matches = list(pat.finditer(body))
    if not matches:
        return body.strip() + "\n"

    parts: list[str] = [body[: matches[0].start()].rstrip()]
    for idx, match in enumerate(matches):
        title = match.group(1)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        compact = _compact_section_content(title, content)
        if "科技" in title and "（无）" in compact:
            continue
        if compact:
            parts.append(f"{title}\n\n{compact}\n")
    return "\n".join(parts).strip() + "\n"


def redact_briefing(body: str, account: dict) -> str:
    """对外分享版：隐藏账户摘要、资产金额、股数、成本与持仓明细。"""
    import re

    body = body.replace("# 交易日报", "# 交易日报（公开分享版）", 1)

    body = _remove_section(body, "## 一、账户与纪律")
    body = _remove_section(body, "## 十、次日执行清单")
    body = _remove_section(body, "## 十三、当前持仓处理")

    body = re.sub(r"\| 已持仓 \|", "| — |", body)
    body = body.replace("不含恒生科技 ETF（513010）", "不含长线 ETF 仓")
    body = body.replace("长线 **513010** 不动", "长线仓不动")
    body = body.replace(
        "*账户更新：每日将券商截图发给 Cursor，说明「请更新 account.yaml」后重新生成本日报。*",
        "",
    )
    body = body.replace(
        "*由 `python scripts/trading/daily_briefing.py` 生成；",
        "*由交易系统自动生成（公开分享版）；",
    )
    return compact_public_briefing(body)


def run_briefing(
    db_path: Path,
    signal_date: Optional[str] = None,
    account_path: Path = ACCOUNT_PATH,
    out_dir: Path = JOURNAL_DIR,
) -> Path:
    store = DataStore(db_path)
    account = load_account(account_path)
    max_pos = int(account.get("strategy_max_positions") or 4)
    cfg = ThemeConfig(max_dragons=max_pos)
    signal_date = signal_date or store.latest_trade_date()
    if not signal_date:
        raise SystemExit("数据库无交易日")

    universe = store.theme_universe_symbols()
    scan = ThemeScanner(store, cfg, symbols=universe).scan(signal_date)
    exclude = list(account.get("long_term_symbols") or [])
    momentum_picks = scan_momentum_picks(
        db_path,
        signal_date,
        top_stocks=int(account.get("momentum_top_stocks") or 4),
        exclude_symbols=exclude,
    )
    rs_watch = scan_relative_strength_watch(store, scan, exclude_symbols=exclude)
    tech_value = scan_tech_gentle_value(
        store,
        signal_date,
        exclude_symbols=exclude,
    )
    engulf = scan_ma5_pullback_engulf(store, scan, exclude_symbols=exclude)
    box = scan_box_breakout(store, scan, exclude_symbols=exclude)
    ma5_watch = scan_ma5_bull_pullback_watch(store, scan, exclude_symbols=exclude)
    top_picks = build_top_picks(
        store,
        signal_date,
        scan,
        momentum_picks,
        rs_watch,
        tech_value,
        engulf,
        box,
        ma5_watch,
    )
    body = build_briefing(
        scan, account, cfg, store, signal_date, momentum_picks, rs_watch, tech_value, engulf, box, ma5_watch, top_picks
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{signal_date}-日报.md"
    out_path.write_text(body, encoding="utf-8")
    public_path = out_dir / f"{signal_date}-日报-公开.md"
    public_path.write_text(redact_briefing(body, account), encoding="utf-8")
    return out_path, public_path


def main():
    parser = argparse.ArgumentParser(description="生成每日交易日报")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default=None, help="信号日 YYYY-MM-DD，默认库内最新")
    parser.add_argument("--account", default=str(ACCOUNT_PATH))
    args = parser.parse_args()
    path, public_path = run_briefing(
        Path(args.db),
        args.date,
        Path(args.account),
    )
    print(f"日报已生成: {path}")
    print(f"公开版已生成: {public_path}")


if __name__ == "__main__":
    main()
