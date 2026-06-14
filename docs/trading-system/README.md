# 短线交易系统（可进化）

> **完整使用说明** → [使用指南.md](使用指南.md)（数据爬取、主策略、AI 复盘/入库/进化、对话模板、技巧）

## 你怎么用

1. **喂 PDF**：摘要放进 `knowledge/`，对话里 @ 该文件并说「入库」
2. **记交易**：每笔追加到 `trades.csv`
3. **算指标**：`python scripts/trading/update_metrics.py`
4. **复盘**：复制 Skill 里的 journal 模板到 `journal/YYYY-MM-DD.md`，或说「今日复盘」
5. **进化**：指标 + 复盘达标后，对 Agent 说「根据数据提议进化」→ 确认后改 playbook / 代码

## Cursor Skill

路径：`.cursor/skills/short-term-trading-evolution/SKILL.md`

触发词示例：短线、复盘、胜率、交易系统进化、playbook

## 目录

```
constitution.md    # 宪法（慎改）
playbooks/         # 可执行规则
knowledge/         # PDF 摘要
trades.csv         # 成交流水
metrics/latest.json
journal/
changelog.md       # 进化史
```
