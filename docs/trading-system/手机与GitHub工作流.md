# 手机 Agent + GitHub 私有库工作流

> **分工**：手机 Cloud Agent 只改 **账户快照** 并 push；**行情库 + 日报生成 + 微信推送** 仍在 Mac 本机。

## 一、为什么这样拆

| 内容 | 是否进 Git | 原因 |
|------|------------|------|
| 代码、playbooks、journal 模板 | ✅ | 版本管理 |
| `account.yaml` | ✅ 私有库 | 手机 Agent 可改持仓 |
| `stock_data.db`（约 4GB） | ❌ | 太大，仅 Mac 本地 |
| `notify.yaml` / `tushare.yaml` | ❌ | 含 token，仅本机 |

Cloud Agent **不能**在你 Mac 上跑 `daily_briefing.py`，所以流程是：**手机改账户 → GitHub → Mac 拉取 → 生成并推送**。

---

## 二、一次性 setup

### 1. 创建 GitHub 私有库

在 [github.com/new](https://github.com/new)：

- Repository name：`quant`（或自定）
- **Private**
- 不要勾选 README（本地已有代码）

### 2. 本机初始化并首次推送

在项目根目录执行（把 `YOUR_USER` 换成你的 GitHub 用户名）：

```bash
cd /Users/guosixu/Documents/quant

git init
git branch -M main
git add .
git status   # 确认没有 notify.yaml、tushare.yaml、stock_data.db
git commit -m "Initial commit: trading system codebase"
git remote add origin git@github.com:YOUR_USER/quant.git
git push -u origin main
```

SSH 未配置时可用 HTTPS：

```bash
git remote add origin https://github.com/YOUR_USER/quant.git
```

### 3. Cursor 连接 GitHub

1. 打开 [cursor.com/dashboard](https://cursor.com/dashboard) → **Integrations** → 连接 **GitHub**
2. 授权访问 **私有库** `quant`
3. 需 **Pro** 及以上才可用 [Cloud Agents](https://cursor.com/agents)

### 4. 手机添加快捷方式（PWA）

1. iPhone Safari 打开 [cursor.com/agents](https://cursor.com/agents)
2. 分享 → **添加到主屏幕**
3. 用同一 Cursor 账号登录

---

## 三、每日收盘后（推荐节奏）

```
17:00 Mac 定时任务     → 用当前 account 生成信号日报 + 推送（市场信号）
       ↓
你发持仓截图到手机 Agent → 更新 account.yaml 并 push
       ↓
Mac 跑 sync 脚本       → pull → 重生成日报 → 再推一条（持仓/执行清单准确）
```

### 步骤 A — 手机（Cloud Agent）

1. 打开 **cursor.com/agents**（或主屏幕 PWA）
2. 选择仓库 **`YOUR_USER/quant`**
3. **上传券商持仓截图**（总资产、可用资金、持仓列表、当日盈亏）
4. 发送下面 **固定提示词**（可复制）：

```
请根据附件券商截图更新 docs/trading-system/account.yaml：
- updated 改为截图日期
- total_capital、cash、daily_pnl_pct
- positions：代码用 sh./sz. 前缀；513010 标 bucket: long_term
- 不要改 notify.yaml、tushare.yaml
改完后 commit 并 push 到 main，回复改了哪些字段。
```

5. 等 Agent 完成（GitHub 上能看到 `account.yaml` 更新）

### 步骤 B — Mac（拉取 + 日报 + 推送）

```bash
/bin/bash scripts/sync_from_github.sh
```

或分步：

```bash
git pull origin main
.venv/bin/python scripts/trading/daily_briefing.py
.venv/bin/python scripts/trading/send_briefing.py
```

---

## 四、仍用 Mac Cursor 时

微信/AirDrop 截图到 Mac → Cursor 对话里发图，同样说：

> 请更新 account.yaml 并重生成日报推送

无需经过 GitHub，适合人就在电脑前的情况。

---

## 五、安全提醒

- 私有库仍含 **持仓与资产**，不要改成 Public
- **不要**把 `notify.yaml`、`tushare.yaml` 提交进 Git
- 若 `account.yaml` 曾误提交到公开库，立即改私有并轮换 token

---

## 六、常见问题

**Q：手机 Agent 说找不到 stock_data.db？**  
A：正常。请它在 GitHub 上**只改 account.yaml**；日报在 Mac 上跑。

**Q：pull 后日报日期不对？**  
A: `daily_briefing.py` 默认用库内最新交易日；先确认 Mac 上 `dataScrapper` 已更新。

**Q：想完全自动 pull？**  
A: 可在 Mac 用 cron 在 17:30 跑 `sync_from_github.sh`（需本机已 `git pull` 无冲突）。

---

## 七、相关文件

| 文件 | 作用 |
|------|------|
| `docs/trading-system/account.yaml` | 账户快照（进私有库） |
| `docs/trading-system/account.yaml.example` | 模板 |
| `scripts/sync_from_github.sh` | Mac 一键 pull + 日报 + 推送 |
| `scripts/daily_stock_update.sh` | 17:00 定时：爬数 + 日报 + 推送 |
