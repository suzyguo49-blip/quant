# 进化协议（Evolution Protocol）

## 原则

- **数据先于观点**：规则变更必须引用 `metrics/latest.json`、回测或 `trades.csv`，不能仅凭单次盈亏。
- **小步迭代**：每次 changelog 最多改 1～3 条规则 id，便于归因。
- **可回滚**：在 changelog 保留「上一版」参数或规则原文。

## 样本量门槛

| 动作 | 最低样本 |
|------|----------|
| 讨论某 setup 好坏 | 该 setup_tag ≥ 10 笔已平仓 |
| 提议删除/降级 setup | ≥ 30 笔，且胜率 < 40% 或 profit_factor < 1.0 |
| 提议新增 setup 入 playbook | 纸面/模拟 ≥ 20 笔 + 回测同向 |
| 修改宪法（constitution） | 用户明确确认 + 书面原因 |

## 晋升（加强 / 加仓许可）

同时满足：

1. `by_setup[tag].win_rate` ≥ 55% 且 `count` ≥ 30
2. 近 10 笔复盘中该 setup 无重大违规
3. 回测（若适用）最大回撤未恶化

→ 在 changelog 标记 `promoted: <tag>`，可在 playbook 中标注「A 级 setup」。

## 降级 / 退役

任一满足即 **提议** 降级（需用户确认）：

1. `by_setup[tag].win_rate` < 40% 且 `count` ≥ 30
2. `max_consecutive_loss` ≥ 5 且均来自同一 setup_tag
3. 连续 3 日复盘标记「违反 <rule_id>」且净亏损

→ changelog 标记 `deprecated: <tag>` 或 `tightened: <rule_id>`。

## 复盘 → 规则 映射

| 复盘标签 | 典型动作 |
|----------|----------|
| `violation:<rule_id>` | 检查 playbook 是否过松；考虑 tightened |
| `good:<rule_id>` | 强化示例写入 `playbooks/review.md` |
| `new_pattern` | 先入 knowledge 观察，未满样本不改 entry |
| `market_regime` | 只记入 journal，不改规则（除非跨 20 日统计） |

## Agent 输出模板（提议进化时）

```markdown
## 进化提议 YYYY-MM-DD

- **类型**: promote | deprecate | tighten | new_rule
- **对象**: <rule_id 或 setup_tag>
- **数据依据**: win_rate=…, n=…, profit_factor=…
- **建议修改**: （playbook 原文草案）
- **风险**: （可能过拟合、样本偏差）
- **需用户确认**: 是
```

用户回复「确认进化」后，才修改 playbooks 与 changelog。
