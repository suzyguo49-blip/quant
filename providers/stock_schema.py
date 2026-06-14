"""stock_data.db 表结构定义与迁移（Tushare 扩展字段）。"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterable, List

logger = logging.getLogger(__name__)

# 原有 + Tushare daily / daily_basic / adj_factor（2000 积分档）
STOCK_DAILY_COLUMNS: List[str] = [
    "symbol",
    "name",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "isST",
    "month_key",
    # daily_basic + adj_factor
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
    "adj_factor",
]

STOCK_DAILY_EXTRA_TYPES: Dict[str, str] = {
    "turnover_rate_f": "REAL",
    "volume_ratio": "REAL",
    "pe": "REAL",
    "pe_ttm": "REAL",
    "pb": "REAL",
    "ps": "REAL",
    "ps_ttm": "REAL",
    "dv_ratio": "REAL",
    "dv_ttm": "REAL",
    "total_share": "REAL",
    "float_share": "REAL",
    "free_share": "REAL",
    "total_mv": "REAL",
    "circ_mv": "REAL",
    "adj_factor": "REAL",
}

STOCK_BASIC_COLUMNS: List[str] = [
    "code",
    "code_name",
    "ipo_date",
    "industry",
    "area",
    "market",
    "exchange",
    "is_hs",
    "list_status",
    "act_name",
    "act_ent_type",
    "cnspell",
    "update_time",
]

STOCK_BASIC_EXTRA_TYPES: Dict[str, str] = {
    "area": "TEXT",
    "market": "TEXT",
    "exchange": "TEXT",
    "is_hs": "TEXT",
    "list_status": "TEXT",
    "act_name": "TEXT",
    "act_ent_type": "TEXT",
    "cnspell": "TEXT",
}

DAILY_BASIC_TS_FIELDS = (
    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,"
    "total_share,float_share,free_share,total_mv,circ_mv"
)

STOCK_BASIC_TS_FIELDS = (
    "ts_code,symbol,name,area,industry,market,exchange,"
    "list_status,list_date,is_hs,act_name,act_ent_type,cnspell"
)


def _add_columns(conn: sqlite3.Connection, table: str, col_types: Dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in col_types.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            logger.info("已扩展 %s.%s (%s)", table, col, typ)


def migrate_stock_schema(db_path: str) -> None:
    """为已有库追加列，不复制数据、不新增表。"""
    conn = sqlite3.connect(db_path)
    try:
        _add_columns(conn, "stock_daily", STOCK_DAILY_EXTRA_TYPES)
        _add_columns(conn, "stock_basic", STOCK_BASIC_EXTRA_TYPES)
        conn.commit()
    finally:
        conn.close()


def vacuum_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        logger.info("VACUUM 完成，已回收替换行空间")
    finally:
        conn.close()
