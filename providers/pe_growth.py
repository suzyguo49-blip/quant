"""业绩改善代理：动态 PE(TTM) < 静态 PE（C-104）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, TypeVar

T = TypeVar("T")


def passes_pe_growth(pe_ttm, pe_static) -> bool:
    """二者均为正且 pe_ttm < pe_static → 近端盈利优于静态年报隐含。"""
    try:
        t = float(pe_ttm)
        s = float(pe_static)
    except (TypeError, ValueError):
        return False
    if t <= 0 or s <= 0:
        return False
    return t < s


class PeGrowthGate:
    """按 signal 日从 stock_daily 读取 pe_ttm / pe 并缓存。"""

    GAP = "动态PE≥静态PE(C-104)"

    def __init__(self, db_path: Path | str, trade_date: str):
        self.trade_date = trade_date
        self._pairs: Dict[str, Tuple[float, float]] = {}
        self._load(db_path)

    def _load(self, db_path: Path | str) -> None:
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT symbol, pe_ttm, pe FROM stock_daily
                    WHERE trade_date = ? AND pe_ttm IS NOT NULL AND pe IS NOT NULL
                    """,
                    (self.trade_date,),
                ).fetchall()
        except sqlite3.OperationalError:
            return
        for sym, ttm, pe in rows:
            self._pairs[str(sym)] = (float(ttm), float(pe))

    def pe_pair(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        p = self._pairs.get(symbol)
        if not p:
            return None, None
        return p[0], p[1]

    def ok(self, symbol: str) -> bool:
        ttm, pe = self.pe_pair(symbol)
        if ttm is None or pe is None:
            return False
        return passes_pe_growth(ttm, pe)

    def filter_symbols(self, symbols: Iterable[str]) -> List[str]:
        return [s for s in symbols if self.ok(s)]

    def filter_dicts(self, rows: List[dict], symbol_key: str = "symbol") -> List[dict]:
        return [r for r in rows if self.ok(r.get(symbol_key, ""))]

    def filter_with_attr(self, rows: List[T], symbol_attr: str = "symbol") -> List[T]:
        return [r for r in rows if self.ok(getattr(r, symbol_attr, ""))]
