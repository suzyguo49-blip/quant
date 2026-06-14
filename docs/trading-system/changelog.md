# 交易系统进化日志

格式：`## [类型] 日期 — 摘要`

类型：`knowledge` | `rule` | `code` | `metric` | `constitution`

---

## [rule+code] 2026-06-14 — GitHub 私有库 + 手机 Agent 更新账户工作流

- **文档**：`docs/trading-system/手机与GitHub工作流.md`
- **脚本**：`scripts/sync_from_github.sh`（Mac：git pull → 日报 → 推送）
- **gitignore**：排除 `stock_data.db`、notify/tushare 密钥；`account.yaml` 进私有库供 Cloud Agent 改

## [rule+code] 2026-06-14 — E-009 次日执行窗口 + 日报第十节执行清单

- **playbook**：新增 `E-009` — 禁止 9:30–10:30 追买；买入窗 10:30–11:00 / 13:00–14:30；高开>3% 放弃；换仓上午先卖
- **日报**：私人版新增 **第十节 次日执行清单**（先卖/后买表、限价参考、失效条件）；换仓/自选/持仓顺延为十一～十三节
- **推送**：从私人日报读取执行清单 + 综合优选详情；策略节仍仅个股名称
- **代码**：`briefing_execution.py`；`theme.md` / `entry.md` 执行表述对齐 E-009

## [rule] 2026-06-14 — manual_themes core 结构调整 + 成分去重

- **core 升格**：T04 光纤光缆、T09 算电协同（5～6 月 scan 高频主线）
- **core 降级**：T14 消费电子 → secondary；T18 AIPC → thematic
- **secondary 升格**：T10 人形机器人
- **成分**：T01 去光纤（归 T04）；T02 删兆易创新；T03 删海光/华虹（归 T11）；T18 与 T02/T03/T11 去重，仅留端侧 unique 标的
- **T11 改名**：半导体设备 → 半导体-AI芯片（设备+设计+封测）
- **文档**：`market_themes_overview.md` tier 表与 6 月盘面摘要

## [rule+code] 2026-05-24 — Top 3 v4：Dragon 优先 + 反包过滤 + 组合出场优化

- **选股 v4**：Dragon·可买 优先占满席位；无 Dragon·可买 时 Top N 最多 1 个替补、总仓 ≤1/3
- **反包**：须 Dragon 主线成分；5 日涨幅 ≤12%；排除 ST / 异常波动公告
- **箱体**：须与 Dragon·可买 同标的共振
- **出场**：反包 −3% 止损 + T-005 满 3 日才破 MA5；浮盈 >8% 止损上移至成本/MA5
- **换仓**：新分须高于持仓分 +5
- **代码**：`briefing_top_picks.py`、`backtest_top_picks.py`（v4 cache/输出路径）

## [rule+code] 2026-06-11 — 箱体突破逻辑优化 + 公开版精简

- **箱体 N-401/N-402 v2**：触顶/底各≥2 次、整理期缩量、收盘突破、量/箱均≥1.5
- **公开/推送版**：去掉换仓/自选，观察池限 5 行，规则说明压缩

## [rule+code] 2026-06-11 — 移除均线粘合 E-003，新增相对抗跌观察 E-008

- **移除**：`ma_convergence_bloom.py`（E-003 静待花开）、X-005/X-006 标记 cancelled
- **新增**：`relative_strength_watch.py` — 大盘 5 日 ≤ −2% 时筛相对抗跌 + 多头趋势（N-501～N-503）
- **共用**：`theme_scan_utils.theme_symbols` 替代原 bloom 模块
- **日报**：第五节改为相对抗跌观察；换仓/一句话去掉「花开」

## [code] 2026-06-06 — 数据源升级 Tushare Pro

- 新增 `providers/tushare_collector.py`：按 `trade_date` 批量拉全市场日线 + 前复权
- `dataScrapper.py`：`make_collector()`，默认 `--source tushare` / `QUANT_DATA_SOURCE`
- `daily_stock_update.sh` 默认同上；配置见 `tushare.yaml.example`

## [code] 2026-06-06 — 修复定时任务推送旧信号日日报

- `daily_stock_update.sh`：覆盖率达标须 **latest ≥ 当日 expected**，避免 6/5 仍用 6/4 数据生成/推送
- 补数结束后若仍缺当日 → `daily-quick` 兜底；仍不足则跳过推送
- `send_briefing.py` 标题改为「信号日YYYY-MM-DD·次日执行」

## [code] 2026-06-06 — 反包 N-302b 量价序列

- `ma5_pullback_engulf.py`：反包前须「放量上涨（阳+量≥1.2×5均量）→ 缩量下跌 1～2 日（阴+量&lt;0.9×5均量）」
- `notes.md` N-302b；日报第七节规则同步

## [code] 2026-06-05 — 五日线反包收紧 N-301 多头排列

- `ma5_pullback_engulf.py`：趋势改为收&gt;MA20 且 MA20&gt;MA60；新增 MA5&gt;MA10&gt;MA20
- 新增 `backtest-day3`：信号日→次日开盘买→第三天收盘胜率
- `notes.md` N-301 v2；日报第七节规则描述同步

## [code] 2026-06-04 — 日报不因补数卡死而跳过

- `daily_stock_update.sh`：补数改后台 + 覆盖率轮询；达标 / 卡死 30min / 超时 150min 后继续生成日报并推送
- 显式 `--date` 传给 `daily_briefing.py` / `send_briefing.py`

## [code] 2026-06-02 — Dragon 支线龙头 T-003b

- `monthly_theme_dragon.py`：人工题材池内 T-001 激活的**次强**题材 → `sub_theme_*` + 支线可买/观察
- `daily_briefing.py` 第二节展示支线；换仓节增加「支线 Dragon 可观察换入」
- `theme.md` 新增 T-003b 规则（观察/换仓参考，不占主线开仓槽）

## [code] 2026-06-02 — Dragon fallback 补位对齐 T-002c

- `monthly_theme_dragon.py`：T-002c 补位 fallback 复用 `_is_theme_position_buy`（须 pct_chg&gt;-2%、乖离&lt;9%、站上 MA10）
- 修复大跌日（如华能 -6.9%）仍被塞入可买列表的问题

## [rule] 2026-06-02 — 新增 T18 AIPC 产业链题材池

- `manual_themes.yaml` T18：芯片/ODM/PCB/存储/封装/散热/端侧软件/测试，共 24 只龙头
- 来源：新浪 AIPC 产业链梳理、金融界联想财报催化（2026-05）

## [init] 2026-05-17 — 初始化 Skill 与 playbook 骨架

- 创建 `short-term-trading-evolution` Skill
- 宪法 v1、E-001 对齐 `MomentumTrend.py`
- 指标脚本 `scripts/trading/update_metrics.py`

## [rule] 2026-05-17 — 不追高：T-002 拆分为观察池 + 回调买

- T-002a 龙头池（watch）/ T-002b 回调买（buy）
- 条件：当日涨幅&lt;5%、乖离&lt;8%、近5日ret5&lt;0
- `monthly_theme_dragon.py` scan 输出【可买】/【观察】
- 回测/模拟仅对 `dragons_buy` 开仓

## [knowledge] 2026-05-17 — 操盘笔记接入通道

- 新增 `knowledge/操盘笔记.md`、`操盘笔记-映射表.md`
- 新增 `playbooks/notes.md`（待粘贴笔记后入库）
- 新增 Cursor 规则 `trading-with-notes.mdc`

## [rule] 2026-05-17 — 月度主线 + 龙头为主策略

- 新增 `playbooks/theme.md`（T-001～T-004）
- 主策略 E-002，`setup_tag=monthly_theme_dragon`
- E-001 降为 watch；宪法增加 C-005（龙头≤2 只）
- 实现 `monthly_theme_dragon.py`（扫描/回测/行业缓存）

## [knowledge] 2026-05-17 — 再扩充题材池（消费电子等）

- 新增 T14 消费电子、T15 家电、T16 创新药、T17 券商非银；去重成分约 170+

## [knowledge] 2026-05-17 — 扩充 manual_themes 成分池

- 11→13 个题材，成分股约 50→120+（去重）；新增 T12 低空、T13 汽智

## [code] 2026-05-18 — 收盘后全量日线自动补全

- `dataScrapper.py daily`：新交易日 + 近 14 日覆盖率 &lt;92% 自动 `retry-failed`（最多 5 轮）
- `daily-quick`：仅增量新日（快）
- launchd 改为 17:00；日志 `logs/launchd_daily.log`

## [code] 2026-05-17 — 账户分层 + 收盘后自动日报

- `account.yaml` 支持 `long_term_symbols`、`strategy_cash_reserve`（5 万底仓）、`bucket: long_term`
- `daily_briefing.py` 区分长线/策略仓；战术现金 = 可用现金 − 底仓
- `daily_stock_update.sh` 在 `daily` 后自动跑 `daily_briefing.py`

## [rule] 2026-05-17 — T-002c 主线建仓（尽量每日持仓）

- 主线激活时：T-002b 优先，不足则 T-002c 按龙头强度建仓（当日涨&lt;5%、乖离&lt;9%）
- `require_daily_position=true`；无主线仍空仓

## [rule] 2026-05-17 — T-002b v3.1 略放宽

- 5 日回调 ≥1.0%（原 1.5%）；乖离 MA20 上限 9%（原 8%）；其余不变

## [rule] 2026-05-17 — T-002b v3 过滤假回调

- 5 日回调 ≥1.5%；当日 pct_chg > -2%；乖离 MA20 在 1%～8%；须站上 MA10

## [rule] 2026-05-17 — 取消 drop_dragon（T-006）

- 持仓不因掉出【可买】/ dragons_buy 而卖；仅靠 MA20、止损、theme_off

## [rule] 2026-05-17 — X-003 出场改 MA20

- `ma_exit`: 10 → **20**；回测卖出原因 `ma20`

## [rule] 2026-05-17 — T-005 最短持有 3 日 + 止损放宽至 -5%

- 买入后 3 个交易日内不因 theme_off / drop_dragon 卖出
- 仍可随时 MA10（X-003）或 -5%（R-003）止损
- `ThemeConfig`: `min_hold_days=3`, `stop_loss_pct=0.05`

## [code] 2026-05-17 — 每日交易日报

- 新增 `scripts/trading/daily_briefing.py` → `journal/日期-日报.md`
- 账户 `account.yaml`；止盈方案 A（MA10 / X-003、X-004）；暂不含买入区间

## [rule] 2026-05-17 — T-001 改为 5 日强确认 + 每晚 scan 定次日

- T-001：`window_days=5`，`min_up_days=4`，`min_sector_return=4%`
- T-004 退出仍用 20 日上涨日 &lt; 10（迟滞，避免频繁开关）
- `scan` 增加【次日策略】；`theme.md` v3

## [knowledge] 2026-05-18 — 静待花开均线粘合战法入库

- 来源：`knowledge/操盘笔记.md` + 网络「均线粘合」通用原理
- 新增 `playbooks/notes.md` N-101～N-105、N-R05～N-R07
- 新增 E-003（`setup_tag=ma_convergence_bloom`）、X-005、X-006；状态均为 **watch**
- 与 T-001 关系：优先在激活主线成分内选股；扫描代码待实现

## [code] 2026-05-18 — 静待花开扫描接入日报

- 新增 `ma_convergence_bloom.py`（`scan_bloom` / CLI `scan`）
- `daily_briefing.py` 第五节：花开可买 + 粘合观察；换仓/一句话含花开信号

## [code] 2026-05-18 — 静待花开改为全市场扫描

- `scan_bloom` 扫描全量 A 股（`load_panel` 无 symbols 限制）
- Dragon 主线仅用于 ★ 标注与排序，不再限制扫描池
- N-101  playbook 同步

## [knowledge] 2026-05-20 — 科技温和放量·相对低估入库

- 用户规则：5 日温和放量上涨 + 动态 PE &lt; 静态 PE + 行业内中下游低估 + 筹码/压力健康 + 科技行业
- 新增 `playbooks/notes.md` N-201～N-204、E-004、X-007/X-008；状态 **watch**

## [code] 2026-05-20 — `tech_gentle_value.py` 与日报第六节

- 新增 `tech_gentle_value.py`（`setup_tag=tech_gentle_value_rise`；`scan` / `refresh-valuation`）
- `daily_briefing.py` 第六节科技价值信号；换仓/一句话含四策略
- `account.yaml` 增加 `tech_value_top: 4`

## [code] 2026-05-20 — 科技价值池扩大题材 ID

- `TECH_THEME_IDS` 增加 T10 机器人、T11 半导体设备、T12 低空经济、T13 汽车智能化

## [rule] 2026-05-24 — T-003 人工题材优先于行业

- 原因：Baostock 行业（如 C35 专用设备）过宽，与交易意图不符
- 5/22 示例：C35 +15% 被 **算电协同-电力运营 +10%** 取代为主线
- 更新 `monthly_theme_dragon.py`、`playbooks/theme.md` v3、`daily_briefing.py` 行业参考行

## [rule] 2026-05-24 — X-007 科技价值出场改 MA60

- `playbooks/exit.md` X-007 v2：收盘 < **MA60**（原 MA20）
- 同步 `tech_gentle_value.py` `TechValueConfig.ma_exit=60`、`daily_briefing.py` 第六节

## [rule] 2026-05-24 — X-007 科技价值停用

- 回测显示底部入场标的易被 MA 出场误杀；**取消 X-007**，仅 X-008 + 止损 -5%
- `TechValueConfig.use_x007=false`；`exit.md` X-007 标记 cancelled

## [knowledge] 2026-05-25 — 五日均线回踩反包入库

- 用户规则：上涨趋势 + 回踩 MA5 + 前日阴/当日阳反包 + 温和放量 + 实体力度
- 新增 `playbooks/notes.md` N-301～N-304、E-005、X-009/X-010；状态 **watch**

## [code] 2026-05-25 — `ma5_pullback_engulf.py` 与日报第七节

- 新增 `ma5_pullback_engulf.py`（`setup_tag=ma5_pullback_engulf`；`scan` / CLI）
- `daily_briefing.py` 第七节反包信号；换仓/一句话含五路策略
- 章节顺延：换仓→八、一句话→九、自选→十、持仓→十一

## [code] 2026-05-25 — `ma5_pullback_engulf` 回测

- 新增 `backtest` 子命令；区间 2025-05-22~2026-05-22 样本：总收益 -6.33%、胜率 36.4%、最大回撤 16.80%
- 出场以 X-009 破 MA5 为主（30 笔）；R-001 止损 25 笔
- 结果见 `docs/trading-system/backtests/ma5_pullback_engulf_*_engulf.*`

<!-- 以下为进化记录，由 Agent 或用户追加 -->
