# Playbook 条目格式

每个 `docs/trading-system/playbooks/*.md` 中的规则使用统一结构：

```markdown
### E-001 名称（简短）

- **setup_tag**: `breakout_20d`（与 trades.csv 一致，英文蛇形）
- **条件**: 可编程/可人工判定的布尔描述
- **过滤**: 不买 ST、成交额 > X 等
- **入场**: 具体价位或时机
- **止损**: 比例或结构位
- **止盈/持有**: 目标或时间止损
- **失效**: 什么情况下规则不适用
- **版本**: v1 | 2026-05-17
- **状态**: active | watch | deprecated
```

## setup_tag 命名

- 与 `trades.csv` 的 `setup_tag` 列**完全一致**
- 新建 tag 前检查 `metrics/latest.json` 的 `by_setup` 是否已存在

## 修改规则时

1. 递增版本或在 id 后加后缀（如 `E-001b`）并 deprecated 旧条
2. 在 `changelog.md` 记录旧版一行摘要
