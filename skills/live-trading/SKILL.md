---
name: live-trading
description: 实盘下单执行（唯一持有 trade-gateway 工具的技能）。当用户**明确要求**"买入/卖出/下单/撤单/查持仓/查委托/执行这个信号"时使用。强制五步铁律：先看交易模式 → 校验风控凭证 → prepare_order 拿确认码 → 用户贴回确认码 → place_order。纯研究意图（值不值得买、研究一下）一律拒绝并转 signal-review；要看信号与调仓清单改用 signal-review；要回测改用 strategy-backtest；要改风控阈值改用 risk-limits。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - quant-data_get_instruments
  - quant-data_get_limit_status
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - trade-gateway_get_trading_mode
  - trade-gateway_get_account
  - trade-gateway_get_positions
  - trade-gateway_get_orders
  - trade-gateway_get_trades
  - trade-gateway_prepare_order
  - trade-gateway_place_order
  - trade-gateway_cancel_order
  - task
argument-hint: "明确的交易指令（买/卖 + 代码 + 数量或金额或目标仓位），或撤单/查持仓"
---

# 实盘交易技能（live-trading）

把"我要买/卖"这类**明确**交易意图，收敛成经**四道代码闸门**约束的下单动作。
**本技能是唯一持有 `trade-gateway_*` 工具的技能**；下单闸门在 MCP（代码）层强制，模型无法绕过。

## 触发逻辑

- **触发方式**：用户**明确要求**执行交易动作（下单/撤单/查实时持仓与委托）。
- **触发示例**：
  - "买入 510300.SH 1000 份，限价 4.0"
  - "把 600519.SH 的 100 股卖掉"
  - "撤掉刚才那笔委托"
  - "执行这个信号，按计划买"
- **不触发**：只想看信号、要调仓清单 → `signal-review`；只是研究"值不值得买" → `market-research`；要回测 → `strategy-backtest`；要调风控阈值 → `risk-limits`。
- **纯研究意图一律拒绝并转 `signal-review`**：用户没有明确说"下单/买入/卖出"时，不得进入下单流程。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| 方向（buy/sell）+ 代码 | 必须有 `code.exchange`；名称先解析成代码 |
| 数量（股/份）或目标金额 | 决定 `qty`；市价单本期未开放，限价单必须给 `price` |
| 关联的 signal_id / review_id | 溯源与风控凭证；缺 `review_id` 闸门会直接拒绝 |

## 工作流程（五步铁律，严格按序，不跳步）

1. **先看模式**：调 `trade-gateway_get_trading_mode`。`mode` 为 `dry_run` 时只写本地 `orders` 表、不报券商；`unavailable` 时仍可走 dry_run 通路演练；`live` 才是真实报单。**未看模式不得进入下一步。**
2. **校验风控凭证**：必须已有 `risk_reviews.verdict='通过'` 的 `review_id`（用 `quant-db_read_query` 确认；无则用 `task` 委托 `risk-reviewer` 复核并落库）。**缺 `risk_review_id` 或结论非"通过"，MCP 层会直接拒绝，不生成确认码。**
3. **生成意图与确认码**：调 `trade-gateway_prepare_order(symbol, side, qty, price, order_type, risk_review_id, strategy_id, signal_id)`，拿到 `intent_id` 与 6 位 `confirm_token`（TTL 120 秒、单次消费）。
4. **把确认码原文展示给用户，等用户贴回**（**关键**）：**模型禁止自行填写、禁止猜测、禁止循环重试**；确认码必须由用户从对话中提供。
5. **下单**：用户贴回确认码后，调 `trade-gateway_place_order(intent_id, confirm_token)`。MCP 会复核 token 时效/消费状态、风控 id、单笔与仓位上限、最小交易单位、涨跌停、停牌。`dry_run=1` 时只写 `orders` 表不报单，返回 `status='dry_run'`。
6. **跟踪与撤单**：用 `trade-gateway_get_orders` / `trade-gateway_get_trades` / `trade-gateway_get_positions` 跟踪；撤单用 `trade-gateway_cancel_order(order_id)`，非可撤状态（`filled`/`cancelled`/`rejected`/`failed`）会被拒绝，且默认只允许撤卖单（`QUANT_CANCEL_SELL_ONLY`）。

## 会被 MCP 层拒绝的情况（闸门拒绝，非异常）

| 拒绝码 | 触发条件 |
|---|---|
| `RISK_REVIEW_REQUIRED` / `RISK_REVIEW_NOT_FOUND` | 缺 `risk_review_id` 或记录不存在 |
| `RISK_REVIEW_REJECTED` | 复核结论非"通过/补充措施后可执行" |
| `RISK_REVIEW_TARGET_MISMATCH` | 复核凭证指向的标的与本次下单不一致 |
| `INTENT_EXPIRED` | 确认码超过 2 分钟 TTL |
| `INTENT_ALREADY_CONSUMED` / `TOKEN_MISMATCH` | token 重复消费 / 确认码不正确 |
| `LOT_SIZE_VIOLATION` | 数量非最小交易单位整数倍（A股/ETF 100、科创板 200） |
| `ORDER_AMOUNT_EXCEEDED` | 单笔金额超 `QUANT_MAX_ORDER_AMOUNT`（默认 100000） |
| `POSITION_CAP_EXCEEDED` | 单标的占比超 `QUANT_MAX_POSITION_PCT`（默认 0.30） |
| `SYMBOL_SUSPENDED` | 标的停牌/退市 |
| `PRICE_ABOVE_LIMIT_UP` / `PRICE_BELOW_LIMIT_DOWN` | 买价高于涨停 / 卖价低于跌停 |
| `MARKET_ORDER_DISABLED` | 本期未开放市价单 |
| `TRADER_UNAVAILABLE` / `DB_UNAVAILABLE` | xtquant/QMT 不可用 / 库不可用 |

## 输出格式

- 使用 `templates/order_template.md` 的结构：交易模式 → 风控凭证 → 下单意图 → **确认码（提示用户贴回）** → 下单结果 → 跟踪与撤单。
- 确认码一律**原文展示**并明确"请把上面 6 位数字贴回确认"。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `下单_{代码}_{日期}.md`。

## 数据口径（写死，不得违背）

- 代码统一 `code.exchange`（`600519.SH` / `510300.SH` / `159915.SZ`）。
- 数量为最小交易单位整数倍：A股/ETF 100 股（份）；科创板 `688` 开头 200 股（`instruments.lot_size` 为准）。
- 成本口径：佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点；`prepare_order` 返回的 `estimated_costs` 为同口径参考实现。
- 信号 `trade_date` 为生成日，**成交必须在下一交易日（T+1）**。

## 禁止事项

- **禁止模型自行填写/猜测/编造确认码**；禁止在用户未贴回确认码前调用 `place_order`；禁止对过期/错误 token 循环重试。
- 禁止跳过 `get_trading_mode` 或跳过风控凭证校验；禁止伪造 `review_id`。
- 禁止把纯研究意图当交易指令执行；**用户明确要求下单才执行**，否则转 `signal-review`。
- 禁止绕过闸门（如直接写 orders/order_intents 表、放宽限额、提高杠杆）；禁止修改 `QUANT_MAX_ORDER_AMOUNT` / `QUANT_MAX_POSITION_PCT` 等环境变量。
- 禁止代替用户决策下单数量与价格；价格与数量以用户确认为准。
- 禁止在 `dry_run` 模式下向用户宣称"已报单"。

## 免责

本项目为研究与工程示例，**不构成任何投资建议**；任何实盘交易风险由用户自行承担。请在充分理解策略、成本与风险后，自行决定是否启用实盘（`-EnableTrading`）并承担全部后果。

## 多智能体协作

下单前的风控凭证只能来自 `risk-reviewer` 子智能体复核（`task` 委托）并落库的 `risk_reviews.verdict='通过'` 记录；本技能**只消费**该凭证，不自行出具结论。
