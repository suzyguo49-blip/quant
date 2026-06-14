---
name: short-term-trading-evolution
description: Guides A-share short-term trading system design, daily review (复盘), win-rate tracking, playbook updates, and strategy code changes in the quant project. Use when the user mentions 短线, 复盘, 胜率, 交易系统进化, PDF教材, playbooks, MomentumTrend, test_short, or wants to evolve trading rules from backtest and journal data.
---

# 短线交易系统进化（A股）

## 系统定位

本 Skill 管理**可版本化的交易知识**，不是一次性聊天记忆。进化闭环：

```
教材/PDF → playbooks → 回测/实盘记录 → 指标(metrics) → 复盘结论 → 修改规则/代码 → changelog
```

**项目路径（固定）**

| 用途 | 路径 |
|------|------|
| 体系宪法（不可频繁改） | `docs/trading-system/constitution.md` |
| 可执行规则 | `docs/trading-system/playbooks/*.md` |
| PDF 摘要原文 | `docs/trading-system/knowledge/*.md` |
| **操盘笔记** | `docs/trading-system/knowledge/操盘笔记.md` → `playbooks/notes.md` |
| 笔记映射 | `docs/trading-system/knowledge/操盘笔记-映射表.md` |
| 交易流水 | `docs/trading-system/trades.csv` |
| 每日复盘 | `docs/trading-system/journal/YYYY-MM-DD.md` |
| **交易日报** | `docs/trading-system/journal/YYYY-MM-DD-日报.md` |
| 账户快照 | `docs/trading-system/account.yaml`（可由截图更新） |
| 指标快照 | `docs/trading-system/metrics/latest.json` |
| 进化日志 | `docs/trading-system/changelog.md` |
| 策略代码 | `monthly_theme_dragon.py`（主）, `MomentumTrend.py`, `test_short.py` |
| 主线规则 | `docs/trading-system/playbooks/theme.md` |
| 行情数据 | `stock_data.db` |

执行指标更新：`python scripts/trading/update_metrics.py`  
生成交易日报：`python scripts/trading/daily_briefing.py`

---

## 何时触发本 Skill

| 用户意图 | 执行工作流 |
|----------|------------|
| 喂 PDF / 教材 | [知识入库](#知识入库) |
| 收盘复盘 | [每日复盘](#每日复盘) |
| 看胜率、统计 | [指标与进化](#指标与进化) |
| 改策略逻辑 | [代码与规则对齐](#代码与规则对齐) |
| 合并冲突规则 | [规则冲突处理](#规则冲突处理) |

---

## 知识入库

用户新增 `docs/trading-system/knowledge/<书名>-summary.md` 或 **`操盘笔记.md`**（已粘贴原文）后：

**操盘笔记**：提炼到 `playbooks/notes.md`（规则 id 用 `N-` 前缀），并更新 `操盘笔记-映射表.md`；与 `theme.md` 的 `T-xxx` 对照，标注「收紧 / 补充 / 冲突」。

1. 阅读 `constitution.md`，标出与宪法冲突的条目（宪法优先，除非用户明确要改宪法）。
2. 将可执行条目写入 `playbooks/entry.md`、`exit.md`、`risk.md`、`review.md`（格式见 [playbook-format.md](playbook-format.md)）。
3. 每条规则必须有 **id**（如 `E-001`）、**可判定条件**、**失效/例外**。
4. 在 `changelog.md` 追加：`## [知识入库] 书名 - 日期 - 新增规则 id 列表`。
5. **禁止**把整本 PDF 塞进 SKILL.md；只保留 playbook 索引。

---

## 每日复盘

0. 若用户发来**账户截图**：更新 `docs/trading-system/account.yaml`（总资产、现金、持仓、当日盈亏%），再运行 `python scripts/trading/daily_briefing.py`。
1. 读取当日 `journal/YYYY-MM-DD-日报.md`（交易计划）；读取 `metrics/latest.json` 与近 20 笔 `trades.csv`。
2. 读取当日/昨日 `journal/YYYY-MM-DD.md`（若不存在，用 [journal-template.md](journal-template.md) 创建复盘）。
3. 对照 `playbooks/review.md` 检查清单逐项填写。
4. 输出必须包含：
   - **做对的 1～3 条**（对应规则 id）
   - **做错的 1～3 条**（违反哪条规则 id）
   - **明日观察池**（符号、触发条件、失效条件）
   - **是否提议修改规则**（是/否；若是，写入 changelog 草案，不直接改宪法）
5. 提醒用户：新成交需追加到 `trades.csv`（格式见 trades 表头）。

---

## 指标与进化

1. 若用户提供了新交易或更新了 `trades.csv`，先运行：
   ```bash
   python scripts/trading/update_metrics.py
   ```
2. 读取 `metrics/latest.json`，关注：
   - `win_rate`（胜率）
   - `profit_factor`（盈亏比）
   - `avg_win` / `avg_loss`
   - `max_consecutive_loss`
   - `by_setup`（按 setup_tag 分组）
3. 进化决策（详见 [evolution-protocol.md](evolution-protocol.md)）：
   - 某 `setup_tag` 样本 ≥ 30 且胜率 < 40% → **提议降级或删除**该 setup
   - 某规则 id 在复盘中连续 3 次被标为违规且亏损 → **提议收紧** playbook
   - 回测夏普/回撤改善且与 playbook 一致 → **提议写入** changelog 并同步代码
4. 任何规则变更必须：
   - 更新 `playbooks/*.md` 对应 id
   - 在 `changelog.md` 写清：原因、数据依据、生效日期
   - 若涉及参数，同步 `MomentumTrend.py` / `test_short.py` 并说明回测区间

---

## 代码与规则对齐

修改 `MomentumTrend.py` 或 `test_short.py` 前：

1. 列出实现的 playbook 规则 id（若无对应 id，先补 playbook 再写代码）。
2. 用 `stock_data.db` 的 `stock_daily` 做回测；说明区间与股票池。
3. 回测结果写入 `journal/` 或单独 `docs/trading-system/backtests/YYYYMMDD.md`。
4. 代码中的 `params` 默认值应与 `playbooks` 一致；不一致时在 changelog 说明。

---

## 规则冲突处理

优先级（从高到低）：

1. `constitution.md`（资金、止损上限、禁止品种）
2. `playbooks/risk.md`
3. `playbooks/entry.md` / `exit.md`
4. `knowledge/*.md`（仅参考，不直接下单）

冲突时：列出两条规则 id，给出合并建议，**等用户确认**后再改 playbook。

---

## 回答风格

- 用中文；结论先行，数据支撑在后。
- 引用规则时写 **id**（如 `R-002`），便于进化追踪。
- 不给保证收益的承诺；强调样本量不足时的统计局限。
- 用户说「进化」「升级系统」时，默认走指标 + changelog 流程，不只给口头建议。

---

## 附加资源

- 进化门槛与晋升/降级：[evolution-protocol.md](evolution-protocol.md)
- 复盘日记模板：[journal-template.md](journal-template.md)
- Playbook 条目格式：[playbook-format.md](playbook-format.md)
- 指标字段说明：[metrics-guide.md](metrics-guide.md)
