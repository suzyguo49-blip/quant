# 指标说明（metrics/latest.json）

由 `scripts/trading/update_metrics.py` 从 `trades.csv` 生成。

| 字段 | 含义 |
|------|------|
| `updated_at` | 最后计算时间 |
| `total_trades` | 已平仓笔数 |
| `win_rate` | 胜率（pnl_pct > 0） |
| `profit_factor` | 总盈利 / \|总亏损\| |
| `avg_win` / `avg_loss` | 平均盈亏% |
| `expectancy` | 期望值 per trade |
| `max_consecutive_loss` | 最大连亏笔数 |
| `by_setup` | 按 setup_tag 分组统计 |
| `by_rule` | 按 primary_rule_id 分组（可选） |
| `recent_20` | 最近 20 笔摘要 |

## trades.csv 列

| 列 | 必填 | 说明 |
|----|------|------|
| trade_id | 是 | 唯一 id |
| symbol | 是 | 如 sh.600000 |
| setup_tag | 是 | 与 playbook 一致 |
| primary_rule_id | 是 | 如 E-001 |
| entry_date | 是 | YYYY-MM-DD |
| exit_date | 是 | 平仓日；未平仓可空 |
| entry_price | 是 | |
| exit_price | 否 | 未平仓空 |
| pnl_pct | 否 | 盈亏%，平仓后填 |
| notes | 否 | 复盘关键词 |

未平仓不计入胜率，但计入 `open_positions` 列表。
