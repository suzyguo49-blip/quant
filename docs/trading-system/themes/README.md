# 人工主线配置

当 Baostock 行业分类不足以表达「概念主线」时，使用 `manual_themes.yaml`。

- **题材总览**（来源与逻辑）：[market_themes_overview.md](market_themes_overview.md)
- **已预置 18 条题材**（2026-06 tier 调整）：`manual_themes.yaml`
- **core（6 条）**：T01/T02/T03/T04/T09/T11

```yaml
themes:
  - name: 示例概念
    symbols:
      - sh.600519
      - sz.000858
```

扫描时：`python monthly_theme_dragon.py scan --date 2026-05-15`

人工池与行业板块一并参与 T-001 检测；**T-003 优先取人工题材**中 5 日涨幅最高者为主线，行业仅作兜底/参考。
