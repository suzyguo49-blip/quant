# 复盘检查清单（Review）

每日收盘后勾选（Agent 复盘时逐项引用 id）：

- [ ] **V-001** 今日成交是否已全部写入 `trades.csv`
- [ ] **V-002** 每笔 `setup_tag` 与 `primary_rule_id` 是否匹配 playbook
- [ ] **V-003** 是否有违反 constitution 的交易
- [ ] **V-004** 止损/止盈是否按 playbook 执行（非主观拖延）
- [ ] **V-005** 是否运行 `python scripts/trading/update_metrics.py` 更新指标
- [ ] **V-006** 样本量不足的 setup 是否避免「因一两笔就改规则」

## 每周额外项（周五）

- [ ] **V-101** 阅读 `metrics/latest.json` 的 `by_setup`
- [ ] **V-102** 对照 `evolution-protocol` 检查是否该提议 promote/deprecate
- [ ] **V-103** 更新 `changelog.md`（若有确认过的规则变更）
