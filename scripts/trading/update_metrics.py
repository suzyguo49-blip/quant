#!/usr/bin/env python3
"""从 trades.csv 计算胜率等指标，写入 metrics/latest.json"""

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / "docs/trading-system/trades.csv"
METRICS = ROOT / "docs/trading-system/metrics/latest.json"


def load_closed_trades():
    if not TRADES.exists():
        return []
    with TRADES.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    closed = []
    open_pos = []
    for r in rows:
        if not r.get("trade_id") or not r.get("symbol"):
            continue
        exit_date = (r.get("exit_date") or "").strip()
        pnl = (r.get("pnl_pct") or "").strip()
        if exit_date and pnl:
            try:
                r["_pnl"] = float(pnl)
                closed.append(r)
            except ValueError:
                pass
        elif not exit_date:
            open_pos.append(
                {
                    "trade_id": r["trade_id"],
                    "symbol": r["symbol"],
                    "setup_tag": r.get("setup_tag", ""),
                    "entry_date": r.get("entry_date", ""),
                }
            )
    return closed, open_pos


def consecutive_losses(pnls):
    best = cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def group_stats(trades, key):
    groups = defaultdict(list)
    for t in trades:
        k = t.get(key) or "unknown"
        groups[k].append(t["_pnl"])
    out = {}
    for k, pnls in groups.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        n = len(pnls)
        out[k] = {
            "count": n,
            "win_rate": round(len(wins) / n, 4) if n else None,
            "avg_pnl": round(sum(pnls) / n, 4) if n else None,
            "profit_factor": round(
                sum(wins) / abs(sum(losses)), 4
            ) if losses and wins else None,
        }
    return out


def main():
    closed, open_pos = load_closed_trades()
    pnls = [t["_pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total = len(pnls)
    metrics = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_trades": total,
        "win_rate": round(len(wins) / total, 4) if total else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4)
        if losses and wins
        else None,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "expectancy": round(sum(pnls) / total, 4) if total else None,
        "max_consecutive_loss": consecutive_losses(pnls),
        "by_setup": group_stats(closed, "setup_tag"),
        "by_rule": group_stats(closed, "primary_rule_id"),
        "open_positions": open_pos,
        "recent_20": [
            {
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "setup_tag": t.get("setup_tag"),
                "pnl_pct": t["_pnl"],
            }
            for t in closed[-20:]
        ],
    }

    METRICS.parent.mkdir(parents=True, exist_ok=True)
    METRICS.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
