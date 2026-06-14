"""次日执行清单（playbook E-009）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from monthly_theme_dragon import (
    DataStore,
    ScanResult,
    ThemeConfig,
)

EXEC_TIME_WINDOWS = "10:30–11:00 或 13:00–14:30"
EXEC_FORBIDDEN = "9:30–10:30 禁止追买"
SELL_TIME_WINDOW = "9:35–10:30（换仓/止损优先）"
GAP_UP_SKIP = 0.03
LIMIT_LO_PCT = -0.01
LIMIT_HI_PCT = 0.02


def _symbol_snapshot(
    store: DataStore, symbol: str, as_of: str, ma_exit: int
) -> Optional[dict]:
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
    name = row.get("name") or symbol
    return {
        "symbol": symbol,
        "name": str(name),
        "close": close,
        "ma_exit": round(ma_line, 2),
    }


@dataclass
class ExecSellItem:
    symbol: str
    name: str
    reason: str


@dataclass
class ExecBuyItem:
    strategy: str
    symbol: str
    name: str
    close: float
    action: str
    limit_lo: float
    limit_hi: float
    skip_if: str
    stop: str


def _gap_skip_text(close: float) -> str:
    hi = round(close * (1 + GAP_UP_SKIP), 2)
    return f"高开>{GAP_UP_SKIP * 100:.0f}%（>{hi:.2f}）放弃"


def _limit_band(close: float) -> tuple[float, float]:
    return round(close * (1 + LIMIT_LO_PCT), 2), round(close * (1 + LIMIT_HI_PCT), 2)


def collect_exec_sell_items(
    strategy_positions: Sequence[dict],
    store: DataStore,
    cfg: ThemeConfig,
    signal_date: str,
) -> List[ExecSellItem]:
    items: List[ExecSellItem] = []
    for p in strategy_positions:
        sym = p.get("symbol", "")
        if not sym:
            continue
        snap = _symbol_snapshot(store, sym, signal_date, cfg.ma_exit)
        if snap and snap["close"] < snap["ma_exit"]:
            items.append(
                ExecSellItem(
                    symbol=sym,
                    name=str(p.get("name") or snap.get("name") or sym),
                    reason=f"破 MA{cfg.ma_exit} {snap['ma_exit']:.2f}（X-003）",
                )
            )
    return items


def collect_exec_buy_items(
    *,
    scan: ScanResult,
    store: DataStore,
    cfg: ThemeConfig,
    signal_date: str,
    strategy_positions: Sequence[dict],
    momentum_picks: Sequence[dict],
    tech_buys: Sequence,
    engulf_buys: Sequence,
    box_buys: Sequence,
    slots: int,
) -> List[ExecBuyItem]:
    held = {p.get("symbol") for p in strategy_positions}
    action = "换仓" if slots <= 0 else "新开"
    seen: set[str] = set()
    items: List[ExecBuyItem] = []

    def add(
        strategy: str,
        symbol: str,
        name: str,
        close: float,
        stop: str,
        *,
        act: Optional[str] = None,
    ) -> None:
        if not symbol or symbol in held or symbol in seen or close <= 0:
            return
        lo, hi = _limit_band(close)
        items.append(
            ExecBuyItem(
                strategy=strategy,
                symbol=symbol,
                name=name,
                close=close,
                action=act or action,
                limit_lo=lo,
                limit_hi=hi,
                skip_if=_gap_skip_text(close),
                stop=stop,
            )
        )
        seen.add(symbol)

    for c in scan.candidates_buy:
        snap = _symbol_snapshot(store, c.symbol, signal_date, cfg.ma_exit)
        if not snap:
            continue
        add(
            "Dragon",
            c.symbol,
            c.name,
            snap["close"],
            f"R-003 −{cfg.stop_loss_pct * 100:.0f}% 或 MA{cfg.ma_exit} {snap['ma_exit']:.2f}",
        )

    for c in scan.sub_candidates_buy or []:
        snap = _symbol_snapshot(store, c.symbol, signal_date, cfg.ma_exit)
        if not snap:
            continue
        add(
            "Dragon支线",
            c.symbol,
            c.name,
            snap["close"],
            f"R-003 −{cfg.stop_loss_pct * 100:.0f}% 或 MA{cfg.ma_exit} {snap['ma_exit']:.2f}",
            act="换仓",
        )

    for m in momentum_picks:
        add(
            "Momentum",
            m["symbol"],
            m["name"],
            float(m["close"]),
            f"R-001 −2% 或 MA20 {float(m['ma20']):.2f}",
        )

    for t in tech_buys:
        add("科技", t.symbol, t.name, float(t.close), "R-001 −2%（X-008 出场）")

    for e in engulf_buys:
        add(
            "反包",
            e.symbol,
            e.name,
            float(e.close),
            f"R-001 −2% 或 MA5 {float(e.ma5):.2f}（X-009）",
        )

    for b in box_buys:
        add(
            "箱体",
            b.symbol,
            b.name,
            float(b.close),
            f"R-001 −2% 或箱底 {float(b.box_low):.2f}（X-011）",
        )

    return items


def format_execution_checklist_section(
    *,
    sell_items: List[ExecSellItem],
    buy_items: List[ExecBuyItem],
    pause: bool,
    c003_block: bool,
    slots: int,
    allow_rotation: bool,
) -> List[str]:
    lines = [
        "## 十、次日执行清单（E-009）",
        "",
        f"- **纪律**：信号日收盘确认 → **次日**执行；{EXEC_FORBIDDEN}",
        f"- **时间窗**：买入 {EXEC_TIME_WINDOWS}；未成交则**当日放弃**",
        "- **T+1**：当日买入不可当日卖；**换仓时上午先卖、10:30 后再买**",
        f"- **槽位**：可开 **{slots}** 个；换仓 {'允许' if allow_rotation else '关闭'}",
    ]
    if pause:
        lines.extend(["", "- **明日**：`pause_new_orders=true`，暂停新开仓。"])
        return lines
    if c003_block:
        lines.extend(["", "- **明日**：C-003 触发，停止开新仓。"])
        return lines

    if sell_items:
        lines.extend(["", "### 先卖（换仓 / 止损）", ""])
        lines.append("| 代码 | 名称 | 原因 | 建议时段 |")
        lines.append("|------|------|------|----------|")
        for s in sell_items:
            lines.append(
                f"| {s.symbol} | {s.name} | {s.reason} | {SELL_TIME_WINDOW} |"
            )

    if buy_items:
        lines.extend(["", "### 后买（10:30 后）", ""])
        lines.append(
            "| 策略 | 代码 | 名称 | 信号收盘 | 限价参考 | 失效条件 | 止损 | 动作 |"
        )
        lines.append(
            "|------|------|------|----------|----------|----------|------|------|"
        )
        for b in buy_items[:12]:
            lines.append(
                f"| {b.strategy} | {b.symbol} | {b.name} | {b.close:.2f} | "
                f"{b.limit_lo:.2f}–{b.limit_hi:.2f} | {b.skip_if} | {b.stop} | {b.action} |"
            )
        lines.extend([
            "",
            "- **条件单**：限价可取区间上限；14:30 前未成交则放弃",
            "- **试仓**：新开仓可先 50% 槽位，次日符合 playbook 再加满",
        ])
    elif not sell_items:
        lines.extend(["", "- **明日**：无可执行买入；观察为主，不抢 10:30 前。"])

    lines.append("")
    return lines
