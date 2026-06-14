"""Baostock 与 Tushare 代码格式互转。"""

from __future__ import annotations


def ts_to_bs(ts_code: str) -> str:
    """000001.SZ -> sz.000001"""
    code, ex = ts_code.upper().split(".")
    prefix = "sh" if ex == "SH" else "sz"
    return f"{prefix}.{code}"


def bs_to_ts(symbol: str) -> str:
    """sz.000001 -> 000001.SZ"""
    prefix, code = symbol.lower().split(".")
    ex = "SH" if prefix == "sh" else "SZ"
    return f"{code}.{ex}"


def is_a_share_bs(symbol: str) -> bool:
    if not symbol or len(symbol) < 8:
        return False
    s = symbol.lower()
    return s.startswith("sh.6") or s.startswith("sz.0") or s.startswith("sz.3")
