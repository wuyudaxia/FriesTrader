# FriesTrader moomoo 版 — 中文配置说明

这是 [YizhiSong/FriesTrader](https://github.com/YizhiSong/FriesTrader)
的 moomoo（富途美国）移植版，给没有 SSN、开不了 Robinhood 的人用。
英文 `README.md` 是完整说明，这里只讲怎么把它跑起来。

## 它是什么

两个每天定时跑的 Claude Code 会话：

- **Phase A**（工作日下午 4:30 中部时间）：从你的 moomoo 自选股 + 一条
  选股器条件 + 当前持仓里筛候选，跑 `scripts/screen_metrics.py` 做机械过滤
  和信号判断，再搜新闻写投资理由，结果写进 `pending_proposals.jsonl`。
  **不下单。**
- **Phase B**（工作日早上 8:35 中部时间，开盘 5 分钟后）：用开盘价复核，
  先跑止损 / 止盈 / 减仓脚本并执行卖出，再跑买入闸门、排序、整股仓位计算，
  最后按 `execution.mode` 下单，所有决定追加到 `trade_log.jsonl`。

三种模式，只能由你手改 `risk_rules.json`：

| `execution.mode` | 下单去哪 | 读哪个账户 |
|---|---|---|
| `dry_run` | 不下单，只记录"本来会怎么做" | 模拟账户 |
| `paper`（默认） | **真的下到 moomoo 模拟账户**（假钱、真成交） | 模拟账户 |
| `live` | 真实账户（还要满足 `paper_min_cycles_before_live` 天数） | 真实账户 |

## 和 Robinhood 原版的主要区别

- moomoo 下单按**股数**，不能按金额买碎股。所以每笔买入 =
  `floor(金额 / 卖一价)` 股；买不起 1 股的直接跳过并记录，**从不向上取整**。
  几百美元的小账户要注意：20% 仓位 = 100 美元，股价超过 100 的股票就买不了。
- 没有"预览订单"接口，改用券商的**可买/可卖数量查询**做下单前检查。
- 如果 moomoo 返回 `need_order_confirm = true`（需要二次确认的风险提示），
  **不会自动确认**，订单作废并记录，留给你自己看。
- 每日/每周已实现盈亏由 `scripts/realized_pnl.py` 从本流水线自己的成交记录算，
  所以这个账户**只能给这套系统用**，别手动在里面交易。
- 财报日期 moomoo 没有现成接口，靠新闻搜索确认；确认不了就记 `unknown`，
  **不拦截买入**。想改成保守拦截，改 `PHASE_B_TASK.md` Step 7 那一句。

## 开户（没有 SSN）

1. 注册 moomoo（Moomoo Financial Inc.）时，**不要选"美国税务居民"**那条路
   （那条路强制要 SSN / ITIN，就是弹窗里那个提示）。选**非美国税务居民**，
   国籍选护照国，材料是**护照 + 地址证明**（租房合同、水电账单、学校信件），
   税表是 W-8BEN。F-1 前 5 个年历年在税务上本来就是非居民，这样填是对的。
2. app 里一直只给美国居民流程的话，试试注册最开始的国家/地区选护照国，
   或者联系客服说要走帮助中心 "international students without SSN" 的活动
   （topic4_524）。
3. 开好之后，在 app 里开通**美股模拟交易**账户。
4. 兜底方案：申请 ITIN（W-7 表，学校国际学生办公室能帮，一般 7–11 周），
   之后就能走居民通道。

## 配置步骤

1. GitHub 上 **Use this template**（或 fork），设成 **private**。
2. Claude Code 里接上 moomoo MCP：
   ```bash
   claude mcp add moomoo-mcp --transport http https://mcp.moomoo.com/mcp
   ```
   第一次会弹 OAuth 授权页。先只给 `quote:read` 和 `trade:read`；
   准备跑 `paper` 时再加 `trade:write`。
3. 在普通 Claude Code 会话里让它调用：
   - `account_authorized_trd_accs` → 真实美股账户的 `acc_id`，填
     `accounts.live_acc_id`
   - `sim_trade_account_list` → 美股模拟账户的 `acc_id` 和 `market_id`，
     填 `accounts.paper_acc_id` / `accounts.paper_market_id`（美股是 100）
4. moomoo app 里建一个自选股分组，把名字原样填进 `universe.watchlist_name`。
5. `universe.supplementary_screen_queries` 自带一条最简条件（美股 + 市值
   20 亿~500 亿）。让 Claude 手动调一次 `quote_stock_screen` 看结果，
   按需要加相对成交量、RSI 等条件（因子 id 见 moomoo 选股器文档）。
6. 你名下其他 moomoo 账户的 `acc_id` 填进 `wash_sale_avoidance.linked_acc_ids`；
   非居民报税的话 wash sale 规则多半不适用，可以直接 `enabled: false`
   （先问报税的人）。
7. 把 `risk_rules.json` 里每个阈值都看一遍。默认值只是示例，不是建议。
8. 在 claude.ai/code 建两个定时任务，提示词直接用 `README.md` 里的
   "Routine prompt templates"，源指向**你自己的**仓库，并把 moomoo MCP
   挂到任务上。
9. **先用 `paper` 跑至少 10 个交易日**（`paper_min_cycles_before_live`），
   每天自己看 `trade_log_recent.md` 和 `trade_log.jsonl`，重点看被拒绝的
   候选和止损触发。
10. 切 `live` 之前，**先在一个定时任务会话里手动下一笔最小的真实订单**，
    确认无人值守时能过交易密码这一关（moomoo 文档说 `trade:write` 需要
    交易密码，托管 MCP 在无人会话里怎么处理没写明）。过不了就把 `paper`
    当信号源，自己照 `trade_log_recent.md` 手动下单。

## 文件

- `risk_rules.json` — 所有硬性规则和账户配置，模型禁止修改。
- `PHASE_A_TASK.md` / `PHASE_B_TASK.md` — 两个阶段的完整说明，每一步调哪个
  moomoo 工具都写明了。
- `scripts/` — 9 个纯标准库 Python 脚本，所有数学都在这里，模型只照抄输出。
  新增的两个：`screen_metrics.py`（Phase A 过滤和信号）、`realized_pnl.py`
  （已实现盈亏）。
- `trade_log_template.jsonl` — 日志行的样子。

## 免责

这不是投资建议，也没有任何证据说明 LLM 选股能跑赢指数。真正起保护作用的是
`risk_rules.json` 里的规则，不是模型的判断。放松规则是你能做的风险最高的改动。
