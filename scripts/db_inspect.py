#!/usr/bin/env python3
"""轻量查看 stock_data.db（不加载全表，适合 2GB+ 大库）"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "stock_data.db"
OUT_DIR = ROOT / "docs" / "data-samples"


def connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def cmd_overview(conn: sqlite3.Connection, db: Path) -> None:
    size_mb = db.stat().st_size / 1024 / 1024
    print(f"数据库: {db}")
    print(f"文件大小: {size_mb:.1f} MB\n")

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    print("表:")
    for (name,) in tables:
        if name == "stock_daily":
            row = conn.execute(
                "SELECT MIN(trade_date), MAX(trade_date), COUNT(*), COUNT(DISTINCT symbol) FROM stock_daily"
            ).fetchone()
            print(f"  - {name}: {row[2]:,} 行 | {row[3]:,} 只股票 | {row[0]} ~ {row[1]}")
        elif name == "stock_basic":
            n = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
            ind = conn.execute(
                "SELECT COUNT(*) FROM stock_basic WHERE industry IS NOT NULL AND industry != ''"
            ).fetchone()[0]
            print(f"  - {name}: {n:,} 行 | 有行业 {ind:,}")
        else:
            n = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
            print(f"  - {name}: {n:,} 行")

    print("\n索引:")
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ):
        print(f"  - {name}")


def cmd_schema(conn: sqlite3.Connection, table: str) -> None:
    print(f"表结构: {table}\n")
    for row in conn.execute(f"PRAGMA table_info({table})"):
        cid, name, typ, notnull, default, pk = row
        print(f"  {name:12} {typ:10} {'PK' if pk else ''} {'NOT NULL' if notnull else ''}")


def cmd_sample(conn: sqlite3.Connection, table: str, limit: int) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    col_sql = ", ".join(cols)
    print(f"SELECT {col_sql} FROM {table} LIMIT {limit}\n")
    cur = conn.execute(f"SELECT {col_sql} FROM [{table}] LIMIT ?", (limit,))
    print("\t".join(cols))
    for row in cur:
        print("\t".join(str(x) if x is not None else "" for x in row))


def cmd_export_basic(conn: sqlite3.Connection, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv

    rows = conn.execute(
        "SELECT code, code_name, ipo_date, industry, update_time FROM stock_basic ORDER BY code"
    )
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "code_name", "ipo_date", "industry", "update_time"])
        w.writerows(rows)
    print(f"已导出 stock_basic → {out}（可用 Excel 打开）")


def cmd_export_daily_sample(
    conn: sqlite3.Connection, out: Path, date: str, limit: int
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv

    rows = conn.execute(
        """
        SELECT symbol, name, trade_date, open, high, low, close, pct_chg, volume, amount, turn
        FROM stock_daily WHERE trade_date = ? ORDER BY amount DESC LIMIT ?
        """,
        (date, limit),
    )
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            ["symbol", "name", "trade_date", "open", "high", "low", "close", "pct_chg", "volume", "amount", "turn"]
        )
        w.writerows(rows)
    print(f"已导出 {date} 成交额 Top{limit} → {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="轻量查看 stock_data.db")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview", help="库大小、表、行数、日期范围")
    s = sub.add_parser("schema", help="表字段")
    s.add_argument("table", choices=["stock_daily", "stock_basic"])

    s = sub.add_parser("sample", help="打印少量样例行")
    s.add_argument("table", choices=["stock_daily", "stock_basic"])
    s.add_argument("--limit", type=int, default=10)

    sub.add_parser("export-basic", help="导出 stock_basic 为 CSV（约 1 万行内）")

    s = sub.add_parser("export-daily", help="导出某日样例 CSV")
    s.add_argument("--date", default=None, help="默认最新交易日")
    s.add_argument("--limit", type=int, default=50)

    args = p.parse_args()
    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"找不到数据库: {db}")

    with connect(db) as conn:
        if args.cmd == "overview":
            cmd_overview(conn, db)
        elif args.cmd == "schema":
            cmd_schema(conn, args.table)
        elif args.cmd == "sample":
            cmd_sample(conn, args.table, args.limit)
        elif args.cmd == "export-basic":
            cmd_export_basic(conn, OUT_DIR / "stock_basic.csv")
        elif args.cmd == "export-daily":
            date = args.date or conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()[0]
            cmd_export_daily_sample(conn, OUT_DIR / f"stock_daily_{date}_top{args.limit}.csv", date, args.limit)


if __name__ == "__main__":
    main()
