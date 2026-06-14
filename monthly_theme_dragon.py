"""
月度主线 + 龙头策略（回调买、不追高）
- 板块近 5 日强确认（T-001）：上涨日 >= 4 且 5 日涨幅 >= 4%，成交额放大
- 龙头池按强度排名；仅当回调到位（T-002b）才给 buy 信号
- 每晚 scan → 决定次日是否开仓（信号日收盘判定，次日执行）
"""

import argparse
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import baostock as bs
import numpy as np
import pandas as pd
import yaml

from providers.pe_growth import PeGrowthGate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "stock_data.db"
MANUAL_THEMES = ROOT / "docs/trading-system/themes/manual_themes.yaml"


@dataclass
class ThemeConfig:
    window_days: int = 5
    min_up_days: int = 4
    min_sector_return: float = 0.04
    min_stocks: int = 5
    amount_ratio: float = 1.1
    max_dragons: int = 4
    pool_watch_size: int = 5
    ma_dragon: int = 20
    ma_exit: int = 20  # X-003 趋势出场均线
    min_up_days_exit: int = 10
    min_amplitude: float = 0.005
    max_daily_pct_chg: float = 5.0  # 当日涨幅上限（%），须 <
    max_ma_bias: float = 0.09  # 收盘相对 20 日线乖离上限（比例）
    min_ret5: float = -0.01  # 近 5 日至少回调 1.0%（过滤假回调）
    min_entry_pct_chg: float = -2.0  # 当日跌幅不超过 2%（pct_chg 须 > -2）
    min_ma_bias: float = 0.01  # 收盘须明显高于 MA20（≥1%）
    require_above_ma10: bool = True  # 收盘须在 MA10 之上
    min_hold_days: int = 3  # 买入后至少持有 N 个交易日
    stop_loss_pct: float = 0.05  # 单笔止损（比例），对齐 R-003
    require_daily_position: bool = True  # T-002c：主线激活时尽量持仓（龙头建仓）
    enable_sub_theme: bool = True  # T-003b：输出次强人工题材支线龙头
    max_sub_dragons: int = 2  # 支线可买/观察展示上限
    sub_theme_min_return: float = 0.02  # 弱支线：5日涨幅下限（未达 T-001 时）
    sub_theme_min_up_days: int = 3  # 弱支线：上涨日下限


@dataclass
class DragonCandidate:
    symbol: str
    name: str
    ret20: float
    ret5: float
    pct_chg: float
    ma_bias: float
    signal: str  # buy | theme | watch


@dataclass
class ScanResult:
    as_of: str
    theme_active: bool
    theme_name: str = ""
    theme_type: str = ""  # industry | manual
    sector_return: float = 0.0
    up_days: int = 0
    window_days: int = 5
    dragons_buy: List[str] = field(default_factory=list)
    dragons_watch: List[str] = field(default_factory=list)
    dragon_names_buy: List[str] = field(default_factory=list)
    dragon_names_watch: List[str] = field(default_factory=list)
    candidates_buy: List[DragonCandidate] = field(default_factory=list)
    candidates_watch: List[DragonCandidate] = field(default_factory=list)
    dragons: List[str] = field(default_factory=list)
    dragon_names: List[str] = field(default_factory=list)
    all_active_themes: List[dict] = field(default_factory=list)
    # T-003b 支线（次强人工题材）
    sub_theme_active: bool = False
    sub_theme_name: str = ""
    sub_theme_type: str = ""
    sub_sector_return: float = 0.0
    sub_up_days: int = 0
    sub_candidates_buy: List[DragonCandidate] = field(default_factory=list)
    sub_candidates_watch: List[DragonCandidate] = field(default_factory=list)
    sub_theme_t001_ok: bool = True  # False = 弱支线（未达 T-001，仅观察）

    def __post_init__(self):
        if not self.dragons_buy and self.dragons:
            self.dragons_buy = list(self.dragons)
        if not self.dragons:
            self.dragons = list(self.dragons_buy)
        if not self.dragon_names_buy and self.dragon_names:
            self.dragon_names_buy = list(self.dragon_names)
        if not self.dragon_names:
            self.dragon_names = list(self.dragon_names_buy)


def is_a_share(code: str) -> bool:
    if not code or len(code) < 8:
        return False
    if code.startswith("sh.000") or code.startswith("sz.399"):
        return False
    return (
        code.startswith("sh.6")
        or code.startswith("sz.0")
        or code.startswith("sz.3")
    )


class DataStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _conn_ro(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def theme_universe_symbols(self) -> List[str]:
        """人工题材池全部成分股（回测加速用）"""
        syms = set()
        for t in self.load_manual_themes():
            syms.update(t.get("symbols") or [])
        return sorted(syms)

    def latest_trade_date(self) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def load_panel(
        self, end_date: str, lookback: int = 80, symbols: Optional[List[str]] = None
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """返回 (日线面板, 行业映射 code->industry)"""
        conn_fn = self._conn_ro if self.db_path.exists() else self._conn
        with conn_fn() as conn:
            dates = pd.read_sql(
                """
                SELECT DISTINCT trade_date FROM stock_daily
                WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
                """,
                conn,
                params=(end_date, lookback),
            )["trade_date"].tolist()
        if not dates:
            return pd.DataFrame(), pd.DataFrame()
        start_date = min(dates)

        with conn_fn() as conn:
            if symbols:
                placeholders = ",".join(["?"] * len(symbols))
                daily = pd.read_sql(
                    f"""
                    SELECT d.symbol, d.name, d.trade_date, d.open, d.high, d.low,
                           d.close, d.pre_close, d.pct_chg, d.volume, d.amount,
                           d.turn, d.isST, b.industry, b.ipo_date
                    FROM stock_daily d
                    LEFT JOIN stock_basic b ON d.symbol = b.code
                    WHERE d.trade_date BETWEEN ? AND ?
                      AND d.symbol IN ({placeholders})
                    """,
                    conn,
                    params=[start_date, end_date] + list(symbols),
                )
            else:
                daily = pd.read_sql(
                    """
                    SELECT d.symbol, d.name, d.trade_date, d.open, d.high, d.low,
                           d.close, d.pre_close, d.pct_chg, d.volume, d.amount,
                           d.turn, d.isST, b.industry, b.ipo_date
                    FROM stock_daily d
                    LEFT JOIN stock_basic b ON d.symbol = b.code
                    WHERE d.trade_date BETWEEN ? AND ?
                    """,
                    conn,
                    params=(start_date, end_date),
                )
        daily["trade_date"] = pd.to_datetime(daily["trade_date"])
        daily = daily[daily["symbol"].map(is_a_share)]
        daily = daily[daily["isST"].fillna(0).astype(int) == 0]
        return daily, daily[["symbol", "industry"]].drop_duplicates("symbol")

    def load_symbol_names(self, symbols: Optional[List[str]] = None) -> Dict[str, str]:
        """symbol -> 简称（取库内最新一条日线 name）"""
        conn_fn = self._conn_ro if self.db_path.exists() else self._conn
        with conn_fn() as conn:
            if symbols:
                placeholders = ",".join(["?"] * len(symbols))
                df = pd.read_sql(
                    f"""
                    SELECT symbol, name FROM stock_daily
                    WHERE symbol IN ({placeholders})
                      AND name IS NOT NULL AND name != ''
                    ORDER BY trade_date DESC
                    """,
                    conn,
                    params=list(symbols),
                )
            else:
                df = pd.read_sql(
                    """
                    SELECT symbol, name FROM stock_daily
                    WHERE name IS NOT NULL AND name != ''
                    ORDER BY trade_date DESC
                    """,
                    conn,
                )
        if df.empty:
            return {}
        return df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()

    def load_manual_themes(self) -> List[dict]:
        if not MANUAL_THEMES.exists():
            return []
        with MANUAL_THEMES.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("themes") or []


class IndustryCache:
    """从 Baostock 刷新行业到 stock_basic.industry"""

    def __init__(self, db_path: Path, sleep_sec: float = 0.15):
        self.db_path = db_path
        self.sleep_sec = sleep_sec

    def refresh(self, limit: Optional[int] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            codes = pd.read_sql(
                "SELECT code FROM stock_basic WHERE code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%'",
                conn,
            )["code"].tolist()
        if limit:
            codes = codes[:limit]

        lg = bs.login()
        if lg.error_code != "0":
            logging.error("Baostock 登录失败: %s", lg.error_msg)
            return 0

        updated = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                for i, code in enumerate(codes):
                    rs = bs.query_stock_industry(code=code)
                    if rs.error_code != "0" or not rs.next():
                        time.sleep(self.sleep_sec)
                        continue
                    row = rs.get_row_data()
                    # updateDate, code, code_name, industry, industryClassification
                    industry = row[3] if len(row) > 3 else ""
                    if industry:
                        conn.execute(
                            "UPDATE stock_basic SET industry = ? WHERE code = ?",
                            (industry, code),
                        )
                        updated += 1
                    if (i + 1) % 200 == 0:
                        conn.commit()
                        logging.info("行业已更新 %s/%s", i + 1, len(codes))
                    time.sleep(self.sleep_sec)
                conn.commit()
        finally:
            bs.logout()
        logging.info("行业刷新完成，更新 %s 条", updated)
        return updated


class ThemeScanner:
    def __init__(self, store: DataStore, cfg: ThemeConfig, symbols: Optional[List[str]] = None):
        self.store = store
        self.cfg = cfg
        self.symbols = symbols

    def _filter_ipo(self, df: pd.DataFrame, as_of: pd.Timestamp, min_days: int = 60) -> pd.DataFrame:
        if "ipo_date" not in df.columns:
            return df
        ipo = pd.to_datetime(df["ipo_date"], errors="coerce")
        return df[(as_of - ipo).dt.days >= min_days]

    def _sector_metrics(
        self, grp: pd.DataFrame, as_of: pd.Timestamp
    ) -> Optional[dict]:
        cfg = self.cfg
        sub = grp[grp["trade_date"] <= as_of].sort_values("trade_date")
        if sub["symbol"].nunique() < cfg.min_stocks:
            return None

        # 等权板块日收益
        px = sub.pivot_table(index="trade_date", columns="symbol", values="close")
        rets = px.pct_change().mean(axis=1).dropna()
        if len(rets) < cfg.window_days:
            return None

        tail = rets.tail(cfg.window_days)
        up_days = int((tail > 0).sum())
        if up_days < cfg.min_up_days:
            return None

        sector_ret = (1 + tail).prod() - 1
        if sector_ret < cfg.min_sector_return:
            return None

        amt = sub.pivot_table(index="trade_date", columns="symbol", values="amount")
        sector_amt = amt.mean(axis=1)
        if len(sector_amt) < 60:
            return None
        recent = sector_amt.tail(5).mean()
        base = sector_amt.tail(60).mean()
        if base <= 0 or recent / base < cfg.amount_ratio:
            return None

        return {
            "up_days": up_days,
            "sector_return": float(sector_ret),
            "amount_ratio": float(recent / base),
        }

    def _sector_metrics_loose(
        self, grp: pd.DataFrame, as_of: pd.Timestamp
    ) -> Optional[dict]:
        """弱支线用：仅算 5 日涨幅/上涨日，不校验 T-001 成交额等"""
        cfg = self.cfg
        sub = grp[grp["trade_date"] <= as_of].sort_values("trade_date")
        if sub["symbol"].nunique() < cfg.min_stocks:
            return None
        px = sub.pivot_table(index="trade_date", columns="symbol", values="close")
        rets = px.pct_change().mean(axis=1).dropna()
        if len(rets) < cfg.window_days:
            return None
        tail = rets.tail(cfg.window_days)
        up_days = int((tail > 0).sum())
        sector_ret = float((1 + tail).prod() - 1)
        return {"up_days": up_days, "sector_return": sector_ret, "amount_ratio": 0.0}

    def _symbol_metrics(self, sdf: pd.DataFrame, as_of: pd.Timestamp) -> Optional[dict]:
        """计算单股在 as_of 日的指标"""
        cfg = self.cfg
        sdf = sdf[sdf["trade_date"] <= as_of].sort_values("trade_date")
        if len(sdf) < cfg.ma_dragon + 1:
            return None
        row = sdf[sdf["trade_date"] == as_of]
        if row.empty:
            return None
        row = row.iloc[-1]
        close = float(row["close"])
        if close <= 0:
            return None
        ma20 = float(sdf["close"].tail(cfg.ma_dragon).mean())
        ma10 = (
            float(sdf["close"].tail(cfg.ma_exit).mean())
            if len(sdf) >= cfg.ma_exit
            else ma20
        )
        if close <= ma20:
            return None
        high, low = float(row.get("high", close) or close), float(row.get("low", close) or close)
        if (high - low) / close < cfg.min_amplitude:
            return None

        ret20 = close / float(sdf["close"].iloc[-21]) - 1 if len(sdf) >= 21 else 0.0
        ret5 = close / float(sdf["close"].iloc[-6]) - 1 if len(sdf) >= 6 else 0.0
        ma_bias = close / ma20 - 1

        pct = row.get("pct_chg")
        if pd.notna(pct):
            pct_chg = float(pct)
        elif pd.notna(row.get("pre_close")) and float(row["pre_close"]) > 0:
            pct_chg = (close / float(row["pre_close"]) - 1) * 100
        else:
            pct_chg = 0.0

        return {
            "symbol": row["symbol"],
            "name": row.get("name", row["symbol"]),
            "close": close,
            "ma10": ma10,
            "ret20": ret20,
            "ret5": ret5,
            "pct_chg": pct_chg,
            "ma_bias": ma_bias,
            "amt5": float(sdf["amount"].tail(5).mean()),
            "turn5": float(sdf["turn"].tail(5).mean()) if "turn" in sdf else 0.0,
        }

    def _is_pullback_buy(self, m: dict) -> bool:
        """T-002b：真回调（过滤贴线买入、大跌日抄底）"""
        cfg = self.cfg
        if m["pct_chg"] >= cfg.max_daily_pct_chg:
            return False
        if m["pct_chg"] <= cfg.min_entry_pct_chg:
            return False
        if m["ma_bias"] >= cfg.max_ma_bias:
            return False
        if m["ma_bias"] < cfg.min_ma_bias:
            return False
        if m["ret5"] >= 0 or m["ret5"] > cfg.min_ret5:
            return False
        if cfg.require_above_ma10 and m.get("close", 0) <= m.get("ma10", 0):
            return False
        return True

    def _is_theme_position_buy(self, m: dict) -> bool:
        """T-002c：主线建仓（站上均线、不追高，不要求 5 日回调）"""
        cfg = self.cfg
        if m["pct_chg"] >= cfg.max_daily_pct_chg:
            return False
        if m["pct_chg"] <= cfg.min_entry_pct_chg:
            return False
        if m["ma_bias"] >= cfg.max_ma_bias:
            return False
        if cfg.require_above_ma10 and m.get("close", 0) <= m.get("ma10", 0):
            return False
        return True

    def _pick_dragons_split(
        self, grp: pd.DataFrame, as_of: pd.Timestamp
    ) -> Tuple[List[DragonCandidate], List[DragonCandidate]]:
        """龙头观察池 + 回调可买（T-002a / T-002b）"""
        cfg = self.cfg
        sub = grp[grp["trade_date"] <= as_of]
        metrics_list = []
        for sym, sdf in sub.groupby("symbol"):
            m = self._symbol_metrics(sdf, as_of)
            if m:
                m["symbol"] = sym
                metrics_list.append(m)

        if not metrics_list:
            return [], []

        df = pd.DataFrame(metrics_list)
        df["pool_score"] = (
            df["ret20"].rank(pct=True) * 0.5
            + df["amt5"].rank(pct=True) * 0.3
            + df["turn5"].rank(pct=True) * 0.2
        )
        pool = df.nlargest(cfg.pool_watch_size, "pool_score")
        score_by_sym = {r["symbol"]: float(r["pool_score"]) for _, r in pool.iterrows()}

        pool_all: List[DragonCandidate] = []
        for _, r in pool.iterrows():
            m = r.to_dict()
            if self._is_pullback_buy(m):
                sig = "buy"
            elif cfg.require_daily_position and self._is_theme_position_buy(m):
                sig = "theme"
            else:
                sig = "watch"
            pool_all.append(
                DragonCandidate(
                    symbol=r["symbol"],
                    name=r["name"],
                    ret20=float(r["ret20"]),
                    ret5=float(r["ret5"]),
                    pct_chg=float(r["pct_chg"]),
                    ma_bias=float(r["ma_bias"]),
                    signal=sig,
                )
            )

        buy_pullback = [c for c in pool_all if c.signal == "buy"]
        buy_pullback.sort(key=lambda c: (c.ret5, c.ma_bias))
        buy_theme = [c for c in pool_all if c.signal == "theme"]
        buy_theme.sort(key=lambda c: -score_by_sym.get(c.symbol, 0))

        buy: List[DragonCandidate] = []
        for c in buy_pullback:
            if len(buy) >= cfg.max_dragons:
                break
            buy.append(c)
        if cfg.require_daily_position:
            have = {c.symbol for c in buy}
            for c in buy_theme:
                if len(buy) >= cfg.max_dragons:
                    break
                if c.symbol not in have:
                    buy.append(c)
                    have.add(c.symbol)
            if len(buy) < cfg.max_dragons:
                for _, r in pool.iterrows():
                    if len(buy) >= cfg.max_dragons:
                        break
                    sym = r["symbol"]
                    if sym in have:
                        continue
                    m = r.to_dict()
                    if not self._is_theme_position_buy(m):
                        continue
                    buy.append(
                        DragonCandidate(
                            symbol=sym,
                            name=r["name"],
                            ret20=float(r["ret20"]),
                            ret5=float(r["ret5"]),
                            pct_chg=float(r["pct_chg"]),
                            ma_bias=float(r["ma_bias"]),
                            signal="theme",
                        )
                    )
                    have.add(sym)

        buy_syms = {c.symbol for c in buy}
        watch_only = [c for c in pool_all if c.symbol not in buy_syms]
        return watch_only, buy

    def _pick_main_and_sub(
        self, candidates: List[dict]
    ) -> Tuple[dict, Optional[dict]]:
        """T-003 主线 + T-003b 支线（人工题材池内次强，须满足 T-001）"""
        manual = sorted(
            [c for c in candidates if c["type"] == "manual"],
            key=lambda x: -x["sector_return"],
        )
        industry = sorted(
            [c for c in candidates if c["type"] == "industry"],
            key=lambda x: -x["sector_return"],
        )
        if manual:
            best = manual[0]
            sub = manual[1] if len(manual) > 1 else None
        elif industry:
            best = industry[0]
            sub = industry[1] if len(industry) > 1 else None
        else:
            ranked = sorted(candidates, key=lambda x: -x["sector_return"])
            best = ranked[0]
            sub = ranked[1] if len(ranked) > 1 else None
        if sub and sub.get("name") == best.get("name"):
            sub = None
        return best, sub

    def _pick_sub_theme_fallback(
        self,
        daily: pd.DataFrame,
        as_of_ts: pd.Timestamp,
        main_name: str,
        t001_names: set,
    ) -> Optional[dict]:
        """仅一条人工题材过 T-001 时：取次强人工题材（弱支线）"""
        cfg = self.cfg
        loose: List[dict] = []
        for theme in self.store.load_manual_themes():
            name = theme.get("name", "manual")
            if name == main_name or name in t001_names:
                continue
            symbols = theme.get("symbols") or []
            grp = daily[daily["symbol"].isin(symbols)]
            if grp.empty:
                continue
            m = self._sector_metrics_loose(grp, as_of_ts)
            if not m:
                continue
            if m["sector_return"] < cfg.sub_theme_min_return:
                continue
            if m["up_days"] < cfg.sub_theme_min_up_days:
                continue
            watch, buy = self._pick_dragons_split(grp, as_of_ts)
            loose.append(
                {
                    "name": name,
                    "type": "manual",
                    "watch": watch,
                    "buy": buy,
                    "t001_ok": False,
                    **m,
                }
            )
        if not loose:
            return None
        return max(loose, key=lambda x: x["sector_return"])

    def scan(self, as_of: Optional[str] = None) -> ScanResult:
        as_of = as_of or self.store.latest_trade_date()
        if not as_of:
            return ScanResult(as_of="", theme_active=False)

        daily, _ = self.store.load_panel(as_of, lookback=90, symbols=self.symbols)
        if daily.empty:
            return ScanResult(as_of=as_of, theme_active=False)

        as_of_ts = pd.Timestamp(as_of)
        daily = self._filter_ipo(daily, as_of_ts)

        candidates = []

        # 行业板块
        for industry, grp in daily.groupby("industry"):
            if not industry or pd.isna(industry):
                continue
            m = self._sector_metrics(grp, as_of_ts)
            if not m:
                continue
            watch, buy = self._pick_dragons_split(grp, as_of_ts)
            candidates.append(
                {
                    "name": industry,
                    "type": "industry",
                    "watch": watch,
                    "buy": buy,
                    **m,
                }
            )

        # 人工主线
        for theme in self.store.load_manual_themes():
            name = theme.get("name", "manual")
            symbols = theme.get("symbols") or []
            grp = daily[daily["symbol"].isin(symbols)]
            if grp.empty:
                continue
            m = self._sector_metrics(grp, as_of_ts)
            if not m:
                continue
            watch, buy = self._pick_dragons_split(grp, as_of_ts)
            candidates.append(
                {"name": name, "type": "manual", "watch": watch, "buy": buy, **m}
            )

        if not candidates:
            return ScanResult(as_of=as_of, theme_active=False, all_active_themes=[])

        best, sub = self._pick_main_and_sub(candidates)
        sub_t001_ok = True
        if self.cfg.enable_sub_theme and not sub and best.get("type") == "manual":
            t001_manual = {
                c["name"] for c in candidates if c.get("type") == "manual"
            }
            sub = self._pick_sub_theme_fallback(
                daily, as_of_ts, best["name"], t001_manual
            )
            if sub:
                sub_t001_ok = False
        watch_list: List[DragonCandidate] = best.get("watch") or []
        buy_list: List[DragonCandidate] = best.get("buy") or []

        sub_watch: List[DragonCandidate] = []
        sub_buy: List[DragonCandidate] = []
        if self.cfg.enable_sub_theme and sub:
            sub_watch = sub.get("watch") or []
            sub_buy = sub.get("buy") or []

        pe_gate = PeGrowthGate(self.store.db_path, as_of)
        buy_list = pe_gate.filter_with_attr(buy_list)
        sub_buy = pe_gate.filter_with_attr(sub_buy)

        return ScanResult(
            as_of=as_of,
            theme_active=True,
            theme_name=best["name"],
            theme_type=best["type"],
            sector_return=best["sector_return"],
            up_days=best["up_days"],
            window_days=self.cfg.window_days,
            dragons_buy=[c.symbol for c in buy_list],
            dragons_watch=[c.symbol for c in watch_list],
            dragon_names_buy=[c.name for c in buy_list],
            dragon_names_watch=[c.name for c in watch_list],
            candidates_buy=buy_list,
            candidates_watch=watch_list,
            dragons=[c.symbol for c in buy_list],
            dragon_names=[c.name for c in buy_list],
            all_active_themes=candidates,
            sub_theme_active=bool(sub),
            sub_theme_name=sub["name"] if sub else "",
            sub_theme_type=sub["type"] if sub else "",
            sub_sector_return=float(sub["sector_return"]) if sub else 0.0,
            sub_up_days=int(sub["up_days"]) if sub else 0,
            sub_candidates_buy=sub_buy[: self.cfg.max_sub_dragons],
            sub_candidates_watch=sub_watch[: self.cfg.max_sub_dragons],
            sub_theme_t001_ok=sub_t001_ok,
        )


def _bt():
    import backtrader as bt
    return bt


def _make_strategy_class():
    bt = _bt()

    class _MonthlyThemeDragonStrategy(bt.Strategy):
        params = dict(signals=None, max_dragons=2, printlog=False)

        def __init__(self):
            self.signals = self.p.signals or {}

        def next(self):
            dt = self.datas[0].datetime.date(0).isoformat()
            sig = self.signals.get(dt, {})
            targets = set(sig.get("dragons_buy") or sig.get("dragons") or [])
            active = sig.get("theme_active", False)

            if not active:
                for data in self.datas:
                    if self.getposition(data).size:
                        self.close(data=data)
                return

            total = self.broker.getvalue()
            if not targets:
                return
            cash_each = total * 0.95 / len(targets)

            for data in self.datas:
                sym = data._name
                pos = self.getposition(data).size
                if sym in targets and pos == 0:
                    size = int(cash_each / data.close[0] / 100) * 100
                    if size > 0:
                        self.buy(data=data, size=size)
    return _MonthlyThemeDragonStrategy


def build_signal_series(
    store: DataStore,
    cfg: ThemeConfig,
    start: str,
    end: str,
    step: int = 1,
    universe: Optional[List[str]] = None,
) -> Dict[str, dict]:
    """按交易日生成信号；step>1 时每隔 N 日重算（中间日沿用上一日）"""
    with store._conn_ro() as conn:
        dates = pd.read_sql(
            """
            SELECT DISTINCT trade_date FROM stock_daily
            WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
            """,
            conn,
            params=(start, end),
        )["trade_date"].tolist()

    scanner = ThemeScanner(store, cfg, symbols=universe)
    signals: Dict[str, dict] = {}
    prev = {"theme_active": False, "dragons": [], "dragons_buy": [], "theme_name": ""}
    for i, d in enumerate(dates):
        if step > 1 and i % step != 0:
            signals[d] = dict(prev)
            continue
        r = scanner.scan(d)
        prev = {
            "theme_active": r.theme_active,
            "dragons": list(r.dragons_buy),
            "dragons_buy": list(r.dragons_buy),
            "dragons_watch": [c.symbol for c in r.candidates_watch[:5]],
            "theme_name": r.theme_name,
        }
        signals[d] = dict(prev)
        if (i + 1) % 20 == 0:
            logging.info(
                "信号进度 %s/%s %s active=%s buy=%s",
                i + 1, len(dates), d, r.theme_active, len(r.dragons_buy),
            )
    return signals


def load_close_prices(
    store: DataStore, symbols: List[str], start: str, end: str
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(symbols))
    with store._conn_ro() as conn:
        df = pd.read_sql(
            f"""
            SELECT symbol, trade_date, close FROM stock_daily
            WHERE trade_date BETWEEN ? AND ? AND symbol IN ({placeholders})
            ORDER BY trade_date
            """,
            conn,
            params=[start, end] + list(symbols),
        )
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.pivot(index="trade_date", columns="symbol", values="close")


def _trading_days_held(entry_date: str, current_date: str, dates: List[str]) -> int:
    """entry 日记为第 0 日；current 与 entry 同日为 0。"""
    if entry_date in dates and current_date in dates:
        return dates.index(current_date) - dates.index(entry_date)
    return (pd.Timestamp(current_date) - pd.Timestamp(entry_date)).days


def _position_exit_reason(
    ent: dict,
    sym: str,
    d: str,
    price: float,
    ma: float,
    active: bool,
    targets: set,
    cfg: ThemeConfig,
    dates: List[str],
) -> Optional[str]:
    """出场原因；min_hold 内仅 stop_loss / ma20。"""
    hold_td = _trading_days_held(ent["entry_date"], d, dates)
    pnl = price / ent["entry_price"] - 1

    if pnl <= -cfg.stop_loss_pct:
        return "stop_loss"
    if not pd.isna(ma) and price < ma:
        return "ma20"
    if hold_td < cfg.min_hold_days:
        return None
    if not active:
        return "theme_off"
    return None


def simulate_trades(
    signals: Dict[str, dict],
    dates: List[str],
    closes: pd.DataFrame,
    cfg: ThemeConfig,
    names: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """按 playbook 模拟买卖，返回 trades DataFrame"""
    positions: Dict[str, dict] = {}
    rows = []

    for d in dates:
        dt = pd.Timestamp(d)
        if dt not in closes.index:
            continue
        sig = signals.get(d, {})
        active = bool(sig.get("theme_active"))
        targets = set(sig.get("dragons_buy") or sig.get("dragons") or [])
        theme = sig.get("theme_name", "")

        for sym in list(positions.keys()):
            if sym not in closes.columns:
                continue
            price = closes.at[dt, sym]
            if pd.isna(price):
                continue
            hist = closes[sym].loc[:dt].dropna()
            ma = hist.tail(cfg.ma_exit).mean() if len(hist) >= cfg.ma_exit else np.nan
            ent = positions[sym]
            reason = _position_exit_reason(
                ent, sym, d, float(price), ma, active, targets, cfg, dates
            )

            if reason:
                ent = positions.pop(sym)
                pnl = (price / ent["entry_price"] - 1) * 100
                rows.append(
                    {
                        "symbol": sym,
                        "name": ent.get("name") or (names or {}).get(sym, ""),
                        "theme_name": ent["theme_name"],
                        "entry_date": ent["entry_date"],
                        "exit_date": d,
                        "entry_price": ent["entry_price"],
                        "exit_price": float(price),
                        "pnl_pct": round(pnl, 4),
                        "exit_reason": reason,
                        "hold_days": _trading_days_held(ent["entry_date"], d, dates),
                    }
                )

        if active and targets:
            slots = cfg.max_dragons - len(positions)
            for sym in targets:
                if slots <= 0:
                    break
                if sym in positions or sym not in closes.columns:
                    continue
                price = closes.at[dt, sym]
                if pd.isna(price) or price <= 0:
                    continue
                positions[sym] = {
                    "entry_date": d,
                    "entry_price": float(price),
                    "theme_name": theme,
                    "name": (names or {}).get(sym, ""),
                }
                slots -= 1

    if dates and positions:
        last = dates[-1]
        dt = pd.Timestamp(last)
        for sym, ent in list(positions.items()):
            if sym not in closes.columns or dt not in closes.index:
                continue
            price = closes.at[dt, sym]
            if pd.isna(price):
                continue
            pnl = (price / ent["entry_price"] - 1) * 100
            rows.append(
                {
                    "symbol": sym,
                    "name": ent.get("name") or (names or {}).get(sym, ""),
                    "theme_name": ent["theme_name"],
                    "entry_date": ent["entry_date"],
                    "exit_date": last,
                    "entry_price": ent["entry_price"],
                    "exit_price": float(price),
                    "pnl_pct": round(pnl, 4),
                    "exit_reason": "end_of_backtest",
                    "hold_days": _trading_days_held(ent["entry_date"], last, dates),
                }
            )

    df = pd.DataFrame(rows)
    if not df.empty and "name" not in df.columns:
        df = _attach_trade_names(df, names or {})
    return df


def _sym_label(sym: str, names: Optional[Dict[str, str]] = None) -> str:
    n = (names or {}).get(sym, "").strip()
    return f"{sym} {n}" if n else sym


def _attach_trade_names(trades: pd.DataFrame, names: Dict[str, str]) -> pd.DataFrame:
    if trades.empty or "symbol" not in trades.columns:
        return trades
    out = trades.copy()
    out.insert(1, "name", out["symbol"].map(lambda s: names.get(s, "")))
    return out


def _lot_shares(cash_for_stock: float, price: float, lot: int = 100) -> int:
    if price <= 0 or cash_for_stock < price * lot:
        return 0
    return int(cash_for_stock / price / lot) * lot


def simulate_portfolio_journal(
    signals: Dict[str, dict],
    dates: List[str],
    closes: pd.DataFrame,
    cfg: ThemeConfig,
    initial_capital: float = 400_000.0,
    commission: float = 0.001,
    invest_ratio: float = 0.95,
    names: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """按日模拟资金账户：收盘价成交、整手、双边佣金。"""
    cash = float(initial_capital)
    positions: Dict[str, dict] = {}
    rows: List[dict] = []
    prev_total = initial_capital

    for d in dates:
        dt = pd.Timestamp(d)
        if dt not in closes.index:
            continue
        sig = signals.get(d, {})
        active = bool(sig.get("theme_active"))
        targets = set(sig.get("dragons_buy") or sig.get("dragons") or [])
        theme = sig.get("theme_name", "")
        actions: List[str] = []

        stock_value = 0.0
        for sym, pos in positions.items():
            if sym in closes.columns:
                px = closes.at[dt, sym]
                if not pd.isna(px):
                    stock_value += pos["shares"] * float(px)
        total_before = cash + stock_value

        for sym in list(positions.keys()):
            if sym not in closes.columns:
                continue
            price = closes.at[dt, sym]
            if pd.isna(price):
                continue
            hist = closes[sym].loc[:dt].dropna()
            ma = hist.tail(cfg.ma_exit).mean() if len(hist) >= cfg.ma_exit else np.nan
            ent = positions[sym]
            reason = _position_exit_reason(
                ent,
                sym,
                d,
                float(price),
                ma,
                active,
                targets,
                cfg,
                dates,
            )

            if reason:
                pos = positions.pop(sym)
                proceeds = pos["shares"] * float(price) * (1 - commission)
                cash += proceeds
                pnl_pct = (float(price) / pos["entry_price"] - 1) * 100
                actions.append(
                    f"卖出 {_sym_label(sym, names)} {pos['shares']}股 @{price:.2f} "
                    f"({reason}, {pnl_pct:+.2f}%)"
                )

        stock_value = sum(
            positions[s]["shares"] * float(closes.at[dt, s])
            for s in positions
            if s in closes.columns and not pd.isna(closes.at[dt, s])
        )
        equity = cash + stock_value

        if active and targets:
            slots = cfg.max_dragons - len(positions)
            per_slot = equity * invest_ratio / cfg.max_dragons
            for sym in targets:
                if slots <= 0:
                    break
                if sym in positions or sym not in closes.columns:
                    continue
                price = closes.at[dt, sym]
                if pd.isna(price) or float(price) <= 0:
                    continue
                budget = min(cash, per_slot)
                shares = _lot_shares(budget, float(price))
                if shares <= 0:
                    continue
                cost = shares * float(price) * (1 + commission)
                if cost > cash:
                    continue
                cash -= cost
                positions[sym] = {
                    "shares": shares,
                    "entry_date": d,
                    "entry_price": float(price),
                    "theme_name": theme,
                    "name": (names or {}).get(sym, ""),
                }
                actions.append(f"买入 {_sym_label(sym, names)} {shares}股 @{price:.2f}")
                slots -= 1

        stock_value = sum(
            positions[s]["shares"] * float(closes.at[dt, s])
            for s in positions
            if s in closes.columns and not pd.isna(closes.at[dt, s])
        )
        total = cash + stock_value
        day_ret = (total / prev_total - 1) * 100 if prev_total > 0 else 0.0
        cum_ret = (total / initial_capital - 1) * 100

        advice_parts: List[str] = []
        if not active:
            advice_parts.append("空仓观望：无激活主线")
        else:
            advice_parts.append(f"主线「{theme}」")
            buy_list = sig.get("dragons_buy") or []
            watch_list = sig.get("dragons_watch") or []
            if buy_list:
                advice_parts.append(
                    f"可买: {','.join(_sym_label(s, names) for s in buy_list)}"
                )
            elif not positions and not cfg.require_daily_position:
                advice_parts.append("无买点，勿追高")
            if watch_list:
                advice_parts.append(
                    f"观察: {','.join(_sym_label(s, names) for s in watch_list[:3])}"
                )
        if actions:
            advice_parts.append("今日执行: " + "; ".join(actions))
        elif positions:
            hold_syms = ",".join(
                f"{_sym_label(s, names)}({positions[s]['shares']}股)"
                for s in positions
            )
            advice_parts.append(f"继续持有: {hold_syms}")
        else:
            advice_parts.append("无交易")

        hold_codes = list(positions.keys()) if positions else []
        hold_names = [
            positions[s].get("name") or (names or {}).get(s, "") for s in hold_codes
        ]
        rows.append(
            {
                "date": d,
                "theme_active": active,
                "theme_name": theme if active else "",
                "advice": " | ".join(advice_parts),
                "actions": "; ".join(actions) if actions else "—",
                "holdings": ",".join(hold_codes) if hold_codes else "现金",
                "holding_names": ",".join(
                    f"{c}({n})" if n else c for c, n in zip(hold_codes, hold_names)
                )
                if hold_codes
                else "现金",
                "cash": round(cash, 2),
                "stock_value": round(stock_value, 2),
                "total": round(total, 2),
                "day_return_pct": round(day_ret, 4),
                "cum_return_pct": round(cum_ret, 4),
            }
        )
        prev_total = total

    return pd.DataFrame(rows)


def run_capital_backtest_report(
    db_path: Path,
    start: str,
    end: str,
    initial_capital: float = 400_000.0,
    step: int = 1,
    save_csv: bool = True,
) -> pd.DataFrame:
    store = DataStore(db_path)
    cfg = ThemeConfig()
    universe = store.theme_universe_symbols()
    logging.info(
        "资金回测 %s ~ %s  初始%.0f元 step=%s",
        start,
        end,
        initial_capital,
        step,
    )
    signals = build_signal_series(store, cfg, start, end, step=step, universe=universe)
    dates = sorted(signals.keys())
    all_syms = set(universe)
    for s in signals.values():
        all_syms.update(s.get("dragons_buy") or s.get("dragons") or [])
        all_syms.update(s.get("dragons_watch") or [])
    closes = load_close_prices(store, sorted(all_syms), start, end)
    name_map = store.load_symbol_names(sorted(all_syms))
    journal = simulate_portfolio_journal(
        signals, dates, closes, cfg, initial_capital=initial_capital, names=name_map
    )
    trades = simulate_trades(signals, dates, closes, cfg, names=name_map)

    print("\n" + "=" * 72)
    print(f"资金回测日记  {start} ~ {end}  期初 {initial_capital:,.0f} 元")
    print("假设：信号日收盘价成交；整手(100股)；佣金双边各0.1%；最多持仓2只、仓位95%")
    print("=" * 72)
    if journal.empty:
        print("回测期内无交易日数据")
        return journal

    final = journal.iloc[-1]
    print(f"\n期末总资产: {final['total']:,.2f} 元")
    print(f"累计收益:   {final['cum_return_pct']:+.2f}%")
    hold_disp = final.get("holding_names") or final["holdings"]
    print(f"期末持仓:   {hold_disp}")
    print(f"现金余额:   {final['cash']:,.2f} 元")

    if not trades.empty:
        print(f"\n完整回合交易 {len(trades)} 笔，胜率 {(trades['pnl_pct'] > 0).mean() * 100:.1f}%")

    print("\n--- 逐日操作与净值 ---")
    for _, row in journal.iterrows():
        print(
            f"{row['date']}  资产{row['total']:>12,.0f}  "
            f"日{row['day_return_pct']:+6.2f}%  累计{row['cum_return_pct']:+6.2f}%"
        )
        print(f"  {row['advice']}")
        if row["actions"] != "—":
            print(f"  → {row['actions']}")

    if save_csv:
        out_dir = ROOT / "docs/trading-system/backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{start}_{end}_cap{int(initial_capital)}".replace("-", "")
        path = out_dir / f"portfolio_journal_{tag}.csv"
        journal.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n逐日明细已保存: {path}")
    return journal


def print_backtest_report(trades: pd.DataFrame, start: str, end: str, step: int):
    print("\n" + "=" * 60)
    print(f"龙头策略回测报告  {start} ~ {end}  (信号步长={step}日)")
    print("=" * 60)
    if trades.empty:
        print("回测期内无完整交易（可能无主线激活或数据不足）")
        return

    wins = trades[trades["pnl_pct"] > 0]
    losses = trades[trades["pnl_pct"] < 0]
    n = len(trades)
    win_rate = len(wins) / n * 100
    avg_win = wins["pnl_pct"].mean() if len(wins) else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) else 0
    expectancy = trades["pnl_pct"].mean()
    pf = (
        wins["pnl_pct"].sum() / abs(losses["pnl_pct"].sum())
        if len(losses) and losses["pnl_pct"].sum() != 0
        else float("inf")
    )

    print(f"交易笔数:     {n}")
    print(f"胜率:         {win_rate:.2f}%  ({len(wins)} 胜 / {len(losses)} 负)")
    print(f"盈亏比(PF):   {pf:.2f}")
    print(f"平均盈利:     {avg_win:.2f}%")
    print(f"平均亏损:     {avg_loss:.2f}%")
    print(f"期望值/笔:    {expectancy:.2f}%")
    print(f"平均持仓天数: {trades['hold_days'].mean():.1f}")
    print(f"最大单笔盈利: {trades['pnl_pct'].max():.2f}%")
    print(f"最大单笔亏损: {trades['pnl_pct'].min():.2f}%")

    print("\n按主线题材:")
    by_theme = trades.groupby("theme_name").agg(
        count=("pnl_pct", "count"),
        win_rate=("pnl_pct", lambda s: (s > 0).mean() * 100),
        avg_pnl=("pnl_pct", "mean"),
    )
    print(by_theme.round(2).to_string())

    print("\n按卖出原因:")
    print(trades.groupby("exit_reason")["pnl_pct"].agg(["count", "mean"]).round(2).to_string())

    print("\n最近 10 笔:")
    cols = ["entry_date", "exit_date", "symbol", "name", "theme_name", "pnl_pct", "exit_reason"]
    cols = [c for c in cols if c in trades.columns]
    print(trades[cols].tail(10).to_string(index=False))
    print("=" * 60)


def run_backtest_report(
    db_path: Path,
    start: str,
    end: str,
    step: int = 5,
    save_csv: bool = True,
):
    store = DataStore(db_path)
    cfg = ThemeConfig()
    universe = store.theme_universe_symbols()
    logging.info("题材成分股 %s 只，生成信号中...", len(universe))

    signals = build_signal_series(store, cfg, start, end, step=step, universe=universe)
    dates = sorted(signals.keys())
    active_days = sum(1 for s in signals.values() if s.get("theme_active"))
    logging.info("交易日 %s 天，主线激活 %s 天", len(dates), active_days)

    all_syms = set(universe)
    for s in signals.values():
        all_syms.update(s.get("dragons_buy") or s.get("dragons") or [])
    closes = load_close_prices(store, sorted(all_syms), start, end)
    name_map = store.load_symbol_names(sorted(all_syms))
    trades = simulate_trades(signals, dates, closes, cfg, names=name_map)
    print_backtest_report(trades, start, end, step)

    if save_csv and not trades.empty:
        out_dir = ROOT / "docs/trading-system/backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{start}_{end}_step{step}".replace("-", "")
        path = out_dir / f"dragon_trades_{tag}.csv"
        trades.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n交易明细已保存: {path}")
    return trades


def load_bt_data(store: DataStore, code: str, start: str, end: str):
    bt = _bt()
    with store._conn() as conn:
        df = pd.read_sql(
            """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM stock_daily WHERE symbol = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            conn,
            params=(code, start, end),
        )
    if df.empty or len(df) < 60:
        return None
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    df["openinterest"] = 0
    return bt.feeds.PandasData(dataname=df, openinterest=-1)


def run_backtest(db_path: Path, start: str, end: str, sample_scan_step: int = 5):
    """简化回测：每隔 sample_scan_step 日重算信号以加速"""
    bt = _bt()
    StrategyCls = _make_strategy_class()
    store = DataStore(db_path)
    cfg = ThemeConfig()

    scanner = ThemeScanner(store, cfg)
    r_end = scanner.scan(end)
    if not r_end.theme_active and not r_end.all_active_themes:
        logging.warning("末日无主线；若行业为空请先运行: python monthly_theme_dragon.py refresh-industry")

    with store._conn() as conn:
        dates = pd.read_sql(
            "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            conn,
            params=(start, end),
        )["trade_date"].tolist()

    signals = {}
    for i, d in enumerate(dates):
        if i < cfg.window_days or i % sample_scan_step != 0:
            signals[d] = signals.get(
                dates[i - 1], {"theme_active": False, "dragons": [], "dragons_buy": []}
            )
            continue
        r = scanner.scan(d)
        signals[d] = {
            "theme_active": r.theme_active,
            "dragons": r.dragons_buy,
            "dragons_buy": r.dragons_buy,
        }

    all_dragons = set()
    for s in signals.values():
        all_dragons.update(s.get("dragons_buy") or s.get("dragons") or [])
    if not all_dragons:
        logging.error("回测期内无龙头信号，请刷新行业数据或扩大区间")
        return

    cerebro = bt.Cerebro()
    cerebro.broker.setcash(100000)
    cerebro.broker.setcommission(commission=0.001)

    loaded = 0
    for code in all_dragons:
        data = load_bt_data(store, code, start, end)
        if data:
            cerebro.adddata(data, name=code)
            loaded += 1
    if loaded == 0:
        logging.error("未加载到龙头行情")
        return

    cerebro.addstrategy(StrategyCls, signals=signals, printlog=False)
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")

    logging.info("回测 %s～%s，加载 %s 只龙头标的", start, end, loaded)
    res = cerebro.run()[0]
    final = cerebro.broker.getvalue()
    print(f"\n初始 100000 → 最终 {final:.2f}，收益率 {(final/100000-1)*100:.2f}%")
    dd = res.analyzers.drawdown.get_analysis()
    if dd and "max" in dd:
        print(f"最大回撤: {abs(dd.max.drawdown):.2f}%")


def print_scan(result: ScanResult, cfg: Optional[ThemeConfig] = None):
    cfg = cfg or ThemeConfig()
    w = cfg.window_days
    print("\n" + "=" * 50)
    print(f"扫描日期: {result.as_of}（收盘后）")
    if not result.theme_active:
        print("结论: 无合格主线 → 空仓")
        print("\n【次日策略】不新开仓；有持仓则按 exit.md / MA20 处理")
        return
    print(f"主线: {result.theme_name} ({result.theme_type})")
    print(
        f"{w}日涨幅: {result.sector_return*100:.2f}% | "
        f"上涨日: {result.up_days}/{w}（T-001 5日强确认）"
    )
    pullback = [c for c in result.candidates_buy if c.signal == "buy"]
    theme_pos = [c for c in result.candidates_buy if c.signal == "theme"]
    print(
        "\n【可买·回调】T-002b (5日回调≥1.0%, 当日跌<2%, 乖离1~9%, 站上MA10/MA20):"
    )
    if pullback:
        for c in pullback:
            print(
                f"  - {c.symbol} {c.name} | 5日{c.ret5*100:.1f}% "
                f"当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
            )
    else:
        print("  （无）")
    print(
        "\n【可买·建仓】T-002c (主线龙头，当日涨<5%、乖离<9%、站上MA10，不要求回调):"
    )
    if theme_pos:
        for c in theme_pos:
            print(
                f"  - {c.symbol} {c.name} | 5日{c.ret5*100:.1f}% "
                f"当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
            )
    elif cfg.require_daily_position and not result.candidates_buy:
        print("  （无）→ 龙头池当日均大涨/大跌，无法建仓")
    else:
        print("  （无）")
    print("\n【观察】龙头池，未入选:")
    if result.candidates_watch:
        for c in result.candidates_watch[:5]:
            print(
                f"  - {c.symbol} {c.name} | 5日{c.ret5*100:.1f}% "
                f"当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
            )
    else:
        print("  （无）")
    if result.sub_theme_active:
        w = cfg.window_days
        t001_note = "" if result.sub_theme_t001_ok else "（弱支线·未达 T-001，仅观察）"
        print(
            f"\n【支线·T-003b】{result.sub_theme_name} ({result.sub_theme_type}) | "
            f"{w}日{result.sub_sector_return*100:.2f}% | "
            f"上涨日 {result.sub_up_days}/{w}{t001_note}"
        )
        print("  定位：观察/换仓参考，不占主线 Dragon 开仓槽")
        if result.sub_candidates_buy:
            print("  支线可买:")
            for c in result.sub_candidates_buy:
                tag = "回调" if c.signal == "buy" else "建仓"
                print(
                    f"    - {c.symbol} {c.name} [{tag}] | 5日{c.ret5*100:.1f}% "
                    f"当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
                )
        else:
            print("  支线可买: （无）")
        if result.sub_candidates_watch:
            print("  支线观察:")
            for c in result.sub_candidates_watch:
                print(
                    f"    - {c.symbol} {c.name} | 5日{c.ret5*100:.1f}% "
                    f"当日{c.pct_chg:.1f}% 乖离{c.ma_bias*100:.1f}%"
                )
    print("\n【次日策略】")
    if result.candidates_buy:
        names = [
            f"{c.symbol} {c.name}" for c in result.candidates_buy[: cfg.max_dragons]
        ]
        print(f"  可开仓（≤{cfg.max_dragons}只）: {', '.join(names)}")
        tags = []
        if pullback:
            tags.append("T-002b回调")
        if theme_pos:
            tags.append("T-002c建仓")
        print(f"  执行: 次日开盘买入；{' + '.join(tags)}；当日涨≥5%不买")
    elif cfg.require_daily_position:
        print("  主线激活但龙头池无法建仓（均大涨/大跌）→ 仅观察")
    else:
        print("  不新开仓：等 T-002b 回调")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="月度主线 + 龙头")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan").add_argument("--date", default=None)
    p_ref = sub.add_parser("refresh-industry")
    p_ref.add_argument("--limit", type=int, default=None, help="仅更新前 N 只（测试用）")

    p_bt = sub.add_parser("backtest", help="回测并统计胜率（推荐）")
    p_bt.add_argument("--start", default="2024-01-01")
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--step", type=int, default=5, help="信号重算间隔交易日")

    p_bbt = sub.add_parser("backtest-bt", help="Backtrader 组合回测（可选）")
    p_bbt.add_argument("--start", default="2024-01-01")
    p_bbt.add_argument("--end", default=None)
    p_bbt.add_argument("--step", type=int, default=5)

    p_cap = sub.add_parser("backtest-capital", help="资金账户逐日回测日记")
    p_cap.add_argument("--start", required=True)
    p_cap.add_argument("--end", default=None)
    p_cap.add_argument("--capital", type=float, default=400_000.0)
    p_cap.add_argument("--step", type=int, default=1)

    args = parser.parse_args()
    store = DataStore(Path(args.db))
    cfg = ThemeConfig()

    if args.cmd == "refresh-industry":
        IndustryCache(Path(args.db)).refresh(limit=args.limit)
    elif args.cmd == "scan":
        universe = store.theme_universe_symbols()
        r = ThemeScanner(store, cfg, symbols=universe).scan(args.date)
        print_scan(r, cfg)
    elif args.cmd == "backtest":
        end = args.end or store.latest_trade_date()
        run_backtest_report(Path(args.db), args.start, end, step=args.step)
    elif args.cmd == "backtest-bt":
        end = args.end or store.latest_trade_date()
        run_backtest(Path(args.db), args.start, end, sample_scan_step=args.step)
    elif args.cmd == "backtest-capital":
        end = args.end or store.latest_trade_date()
        run_capital_backtest_report(
            Path(args.db),
            args.start,
            end,
            initial_capital=args.capital,
            step=args.step,
        )


if __name__ == "__main__":
    main()
