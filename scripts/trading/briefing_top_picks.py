"""日报综合优选：基本面 × 技术面 × 消息面 Top N（v4：Dragon 优先 + 反包过滤）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from box_breakout import BoxScanResult
from ma5_bull_pullback_watch import Ma5WatchScanResult
from ma5_pullback_engulf import EngulfScanResult
from monthly_theme_dragon import DataStore, ScanResult
from providers.akshare_news import CAUTION_KW, AkShareNewsProvider, score_snapshot
from providers.pe_growth import PeGrowthGate
from relative_strength_watch import RsWatchScanResult
from tech_gentle_value import TechValueScanResult

logger = logging.getLogger(__name__)


@dataclass
class TopPicksConfig:
    """Top 3 v4 规则。"""
    require_dragon_theme: bool = True
    box_requires_theme: bool = True          # 箱体仅 Dragon 主线激活时入池
    box_resonance_only: bool = True          # 箱体须与 Dragon·可买 共振（同标的）
    exclude_st: bool = True
    engulf_theme_only: bool = True           # 反包须属当日 Dragon 主线成分
    engulf_max_mom5_pct: float = 12.0        # 反包 5 日涨幅硬上限
    engulf_exclude_caution_news: bool = True # 反包排除异常波动类公告/新闻
    no_dragon_max_slots: int = 1             # 无 Dragon·可买 时 Top N 上限
    mom5_chase_pct: float = 15.0
    mom5_chase_penalty: float = 12.0
    mom1_streak_exclude: int = 3
    repeat_cooldown_days: int = 5
    momentum_bonus: Tuple[float, ...] = (8.0, 6.0, 4.0, 2.0)
    rotation_min_score_gap: float = 5.0      # 换仓最低分数差（回测组合用）


SIGNAL_PRIORITY = (
    "Dragon·可买",
    "MA5反包",
    "科技价值",
    "Dragon支线",
    "箱体突破",
)


def signal_priority(signals: List[str]) -> int:
    for i, tag in enumerate(SIGNAL_PRIORITY):
        if tag in signals:
            return i
    return 99


@dataclass
class TopPicksState:
    """跨日状态（回测 / 连续生成日报时用）。"""
    mom1_streak: Dict[str, int] = field(default_factory=dict)
    recent_top3: List[Tuple[str, str]] = field(default_factory=list)  # (signal_date, symbol)

    def update_after(self, signal_date: str, picks: List["PickCandidate"], momentum_picks: Optional[List[dict]]):
        top_syms = {p.symbol for p in picks}
        self.recent_top3 = [(d, s) for d, s in self.recent_top3 if d != signal_date]
        for p in picks:
            self.recent_top3.append((signal_date, p.symbol))

        leader = (momentum_picks or [{}])[0].get("symbol") if momentum_picks else None
        next_streak: Dict[str, int] = {}
        if leader:
            next_streak[leader] = self.mom1_streak.get(leader, 0) + 1
        self.mom1_streak = next_streak

    def cooldown_symbols(self, signal_date: str, trade_dates: List[str], days: int) -> Set[str]:
        if signal_date not in trade_dates:
            return set()
        idx = trade_dates.index(signal_date)
        start = max(0, idx - days)
        window = set(trade_dates[start:idx])
        return {sym for d, sym in self.recent_top3 if d in window}

    def mom1_excluded(self, cfg: TopPicksConfig) -> Set[str]:
        return {sym for sym, n in self.mom1_streak.items() if n >= cfg.mom1_streak_exclude}


@dataclass
class PickCandidate:
    symbol: str
    name: str
    fund_score: float
    tech_score: float
    news_score: float
    composite: float
    fund_note: str
    tech_note: str
    news_note: str
    signals: List[str] = field(default_factory=list)


@dataclass
class TopPicksResult:
    as_of: str
    picks: List[PickCandidate] = field(default_factory=list)
    all_candidates: List[PickCandidate] = field(default_factory=list)
    news_count: int = 0
    symbols_scored: int = 0
    empty_reason: str = ""


def _is_st_stock(store: DataStore, symbol: str, as_of: str) -> bool:
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            """
            SELECT isST, name FROM stock_daily
            WHERE symbol = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            (symbol, as_of),
        ).fetchone()
    if not row:
        return False
    is_st, name = row[0], str(row[1] or "")
    if int(is_st or 0) == 1:
        return True
    return name.startswith("*ST") or name.startswith("ST") or "ST" in name[:4]


def _has_caution_text(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    return any(k in blob for k in CAUTION_KW)


def _is_standalone_box(signals: List[str]) -> bool:
    return "箱体突破" in signals and "Dragon·可买" not in signals


def select_tiered_picks(
    picks: List[PickCandidate],
    top_n: int,
    cfg: TopPicksConfig,
) -> List[PickCandidate]:
    """Dragon·可买 优先占满席位；无 Dragon·可买 时最多 no_dragon_max_slots 个替补。"""
    dragon = [p for p in picks if "Dragon·可买" in p.signals]
    others: List[PickCandidate] = []
    for p in picks:
        if "Dragon·可买" in p.signals:
            continue
        if cfg.box_resonance_only and _is_standalone_box(p.signals):
            continue
        others.append(p)

    if dragon:
        out = dragon[:top_n]
        seen = {p.symbol for p in out}
        for p in others:
            if len(out) >= top_n:
                break
            if p.symbol not in seen:
                out.append(p)
                seen.add(p.symbol)
        return out[:top_n]

    return others[: max(0, cfg.no_dragon_max_slots)]


def score_fundamental(store: DataStore, symbol: str, as_of: str) -> Tuple[float, str]:
    gate = PeGrowthGate(store.db_path, as_of)
    ttm, pe = gate.pe_pair(symbol)
    if not gate.ok(symbol):
        if ttm is None or pe is None:
            return 35.0, "缺 PE 数据"
        return 25.0, f"未过 C-104（PE(TTM){ttm:.1f} vs PE(静){pe:.1f}）"
    improve = (pe - ttm) / pe if pe and pe > 0 else 0
    score = 55.0 + min(45.0, improve * 200)
    return score, f"C-104 成立，PE(TTM){ttm:.1f} < PE(静){pe:.1f}"


def collect_buy_pool(
    scan: ScanResult,
    momentum_picks: Optional[List[dict]],
    tech_value: Optional[TechValueScanResult],
    engulf: Optional[EngulfScanResult],
    box: Optional[BoxScanResult],
    cfg: TopPicksConfig,
) -> Dict[str, dict]:
    """仅可买信号进池；优先级 Dragon·可买 > 反包 > 科技；箱体仅主线激活。"""
    pool: Dict[str, dict] = {}

    def add(sym: str, name: str, tech: float, tag: str):
        if not sym:
            return
        cur = pool.get(sym, {"name": name, "tech": 0.0, "signals": [], "mom_bonus": 0.0, "mom5_pct": None})
        cur["name"] = name or cur["name"]
        cur["tech"] = max(cur["tech"], tech)
        if tag not in cur["signals"]:
            cur["signals"].append(tag)
        pool[sym] = cur

    for c in scan.candidates_buy or []:
        add(c.symbol, c.name, 95.0, "Dragon·可买")
    for c in (engulf.candidates_engulf if engulf else []) or []:
        if cfg.engulf_theme_only and not c.in_active_theme:
            continue
        add(c.symbol, c.name, 88.0, "MA5反包")
    for c in (tech_value.candidates_buy if tech_value else []) or []:
        add(c.symbol, c.name, 82.0, "科技价值")
    for c in scan.sub_candidates_buy or []:
        add(c.symbol, c.name, 80.0, "Dragon支线")
    if scan.theme_active and (not cfg.box_requires_theme or scan.theme_active):
        for c in (box.candidates_breakout if box else []) or []:
            if cfg.box_resonance_only and c.symbol not in pool:
                continue
            if cfg.box_resonance_only and "Dragon·可买" not in pool.get(c.symbol, {}).get("signals", []):
                continue
            add(c.symbol, c.name, 75.0, "箱体突破")

    bonuses = cfg.momentum_bonus
    for i, m in enumerate(momentum_picks or []):
        sym = m.get("symbol")
        if not sym or sym not in pool:
            continue
        bonus = bonuses[i] if i < len(bonuses) else 0.0
        pool[sym]["mom_bonus"] = max(float(pool[sym]["mom_bonus"]), bonus)
        pool[sym]["mom5_pct"] = m.get("mom5_pct")
        tag = f"动量加分#{i + 1}"
        if tag not in pool[sym]["signals"]:
            pool[sym]["signals"].append(tag)

    return pool


def build_top_picks(
    store: DataStore,
    signal_date: str,
    scan: ScanResult,
    momentum_picks: Optional[List[dict]] = None,
    rs_watch: Optional[RsWatchScanResult] = None,
    tech_value: Optional[TechValueScanResult] = None,
    engulf: Optional[EngulfScanResult] = None,
    box: Optional[BoxScanResult] = None,
    ma5_watch: Optional[Ma5WatchScanResult] = None,
    top_n: int = 3,
    skip_news: bool = False,
    cfg: Optional[TopPicksConfig] = None,
    state: Optional[TopPicksState] = None,
    trade_dates: Optional[List[str]] = None,
) -> TopPicksResult:
    cfg = cfg or TopPicksConfig()
    state = state or TopPicksState()

    if cfg.require_dragon_theme and not scan.theme_active:
        return TopPicksResult(
            as_of=signal_date,
            empty_reason="无 Dragon 主线（T-004）→ 不出 Top 3",
            all_candidates=[],
        )

    pool = collect_buy_pool(scan, momentum_picks, tech_value, engulf, box, cfg)
    if not pool:
        return TopPicksResult(
            as_of=signal_date,
            empty_reason="可买池为空（无 Dragon/反包/科技/支线/箱体可买）",
            all_candidates=[],
        )

    excluded = set(state.mom1_excluded(cfg))
    if trade_dates:
        excluded |= state.cooldown_symbols(signal_date, trade_dates, cfg.repeat_cooldown_days)

    news_provider = None if skip_news else AkShareNewsProvider(lookback_days=5, request_delay=0.25)
    picks: List[PickCandidate] = []
    for sym, meta in pool.items():
        if sym in excluded:
            continue
        if cfg.exclude_st and _is_st_stock(store, sym, signal_date):
            continue
        tags = list(meta["signals"])
        mom5 = meta.get("mom5_pct")
        if "MA5反包" in tags and mom5 is not None and float(mom5) > cfg.engulf_max_mom5_pct:
            continue
        fund, fund_note = score_fundamental(store, sym, signal_date)
        tech = float(meta["tech"]) + float(meta.get("mom_bonus") or 0.0)
        tech = min(100.0, tech)

        chase_penalty = 0.0
        if mom5 is not None and float(mom5) > cfg.mom5_chase_pct:
            chase_penalty = cfg.mom5_chase_penalty

        if skip_news:
            news, news_note = 50.0, "回测略过消息面"
        else:
            snap = news_provider.fetch(sym, signal_date)
            scored = score_snapshot(snap, lookback_days=news_provider.lookback_days)
            news, news_note = scored.score, scored.note
            if (
                cfg.engulf_exclude_caution_news
                and "MA5反包" in tags
                and _has_caution_text(news_note, *snap.notice_titles, *snap.news_titles)
            ):
                continue

        composite = fund * 0.35 + tech * 0.45 + news * 0.20 - chase_penalty
        picks.append(
            PickCandidate(
                symbol=sym,
                name=str(meta["name"]),
                fund_score=round(fund, 1),
                tech_score=round(tech, 1),
                news_score=round(news, 1),
                composite=round(composite, 1),
                fund_note=fund_note,
                tech_note="；".join(meta["signals"][:4]),
                news_note=news_note,
                signals=list(meta["signals"]),
            )
        )

    picks.sort(key=lambda p: (-p.composite, signal_priority(p.signals)))
    final = select_tiered_picks(picks, top_n, cfg)
    return TopPicksResult(
        as_of=signal_date,
        picks=final,
        all_candidates=picks,
        news_count=0 if skip_news else news_provider.items_fetched,
        symbols_scored=len(pool),
        empty_reason="" if final else "过滤后无候选（冷却/动量连霸/追高分）",
    )


def format_top_picks_section(result: TopPicksResult) -> List[str]:
    lines = [
        "## 零、综合优选 Top 3（基本面 × 技术面 × 消息面）",
        "",
        "- **方法 v4**：**仅可买池**；**Dragon·可买 优先占满席位**；无 Dragon·可买 时最多 **1** 个替补仓",
        "- **反包**：须属 Dragon 主线成分；5 日涨幅 ≤12%；排除 ST / 异常波动公告",
        "- **箱体**：须与 Dragon·可买 **共振**（同标的）；无主线不出手",
        "- **评分**：基本面=C-104+PE改善，技术面=可买信号+动量加分，消息面=公告+新闻",
        "- **仓位**：最多 3 只；无 Dragon·可买 时总仓 ≤1/3；换仓需新分 **高于** 持仓分 **+5**",
        "- **出场**：Dragon −5% / 反包 −3%；反包 **满 3 日** 后才看破 MA5；浮盈 >8% 止损上移至成本/MA5",
        f"- **新闻源**：候选 **{result.symbols_scored}** 只，近5日命中 **{result.news_count}** 条",
        "",
    ]
    if not result.picks:
        reason = result.empty_reason or "当日无合格候选"
        lines.append(f"- **结论**：（无）— {reason}")
        lines.append("")
        return lines

    lines.extend([
        "| 排名 | 代码 | 名称 | 综合 | 基本面 | 技术面 | 消息面 | 要点 |",
        "|------|------|------|------|--------|--------|--------|------|",
    ])
    for i, p in enumerate(result.picks, 1):
        note = f"技:{p.tech_note[:20]}；消:{p.news_note[:24]}"
        lines.append(
            f"| {i} | {p.symbol} | {p.name} | **{p.composite:.0f}** | "
            f"{p.fund_score:.0f} | {p.tech_score:.0f} | {p.news_score:.0f} | {note} |"
        )
    lines.append("")
    for i, p in enumerate(result.picks, 1):
        lines.append(
            f"- **#{i} {p.symbol} {p.name}**：基本面 {p.fund_note}；"
            f"技术面 {p.tech_note}；消息面 {p.news_note}"
        )
    lines.append("")
    return lines
