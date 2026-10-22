---
name: strategy-backtest
description: 策略历史回测与绩效评估。当用户说"回测""跑一下这个策略的历史表现""年化多少/夏普/最大回撤""调参看看""做个样本外验证"时使用。必须写 `backtests` + `equity_curve`，做成本模型与信号后移对照，并委托 `risk-reviewer` 复核后再交付结论。要选股打分建候选池改用 factor-screening；要把回测结果变成调仓执行清单改用 signal-review；要去实盘下单改用 live-trading。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - quant-data_get_trading_calendar
  - quant-data_get_daily_bars
  - quant-data_get_fundamentals
  - quant-data_get_limit_status
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - task
argument-hint: "策略定义（universe + 因子 + 调仓规则 + 参数）+ 回测区间 + 基准"
---

# 策略回测技能（strategy-backtest）

把"这策略历史上能赚多少"收敛成**可复现、含成本、防偏差、经独立复核**的回测结论。
本技能产出回测结果，**不产生实盘指令**。

## 触发逻辑

- **触发方式**：给定策略定义与区间，跑历史回测并给出绩效指标。
- **触发示例**：
  - "回测一下宽基 ETF 动量轮动，2020 到 2025"
  - "这个策略年化多少、夏普多少、最大回撤多大"
  - "帮我把 top_n 从 2 调到 3 看看"
  - "做个样本外验证，看看是不是过拟合"
- **不触发**：要选股筛选/建候选池 → `factor-screening`；要把回测结果变成今天的调仓执行清单 → `signal-review`；要去实盘下单 → `live-trading`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| 策略定义 | universe + 因子 + 调仓规则 + 参数；缺失则无法复现 |
| 回测区间与样本类型 | `in`/`out`/`walk_forward`，决定过拟合评估是否成立 |
| 基准 | 无则默认宽基（如 000300.SH） |
| 成本假设 | 佣金/印花税/过户费/滑点；缺失按默认口径代入并声明 |

## 工作流程（严格按序，不跳步）

1. **确认库与数据快照**：`quant-db_list_tables` 看表齐全；`quant-db_read_query` 读 `strategies` 拿 `universe_json`/`params_json`/`risk_limits_json`。记录本次回测的 **`data_snapshot_date`**（所用数据的最新交易日）。
2. **构建 universe**：`quant-data_get_instruments`（`include_delisted=True`）与 `quant-db_read_query` 组装成分，**必须保留 `delist_date` 非空标的**（防幸存者偏差）；用指数成分时声明 `point_in_time=false`。
3. **取数**：`quant-data_get_daily_bars`（价格用 `qfq`/`hfq`；**量恒为未复权原始口径**）；`quant-data_get_fundamentals(symbols, as_of_date=调仓日)` **只用 `pub_date <= 调仓日` 的记录**（防前视）；用 `quant-data_get_trading_calendar` 定调仓日与 T+1 交易日；用 `quant-data_get_limit_status` 判定停牌/一字板/涨跌停越界，**不可判定即视为不可交易**。
4. **信号与调仓**：按 `rebalance_rule` 生成信号。**信号 `trade_date` 为生成日，成交必须在下一交易日（T+1）；绝不允许用当日收盘价成交。**
5. **成本模型（必须有）**：佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点；写入 `backtests.cost_model_json`。同口径参考实现见 trade-gateway 的 `estimate_costs`。**缺失成本模型 → 风控直接判"不建议执行"。** 交易单位：A股/ETF 100 股（份），科创板 `688` 开头 200 股。
6. **计算指标**：年化收益 `annual_return`、年化波动 `annual_vol`、最大回撤 `max_drawdown` 与持续期 `max_dd_duration`、`sharpe`、`calmar`、`win_rate`、`profit_loss_ratio`、年换手 `turnover`；记录 **`param_grid_size`**（参数网格规模，供过拟合评估）。
7. **信号后移对照（必须）**：把信号**整体后移 1 个交易日**重跑，比较收益是否由正转负；把结论写入 `backtests.lag_shift_checked`（1=已做且通过对照）。未做对照直接降档。
8. **落库**：写 `backtests`（含 `run_id, strategy_id, start_date, end_date, sample_type, cost_model_json, benchmark, 各项指标, param_grid_size, data_snapshot_date, repro_hash, lag_shift_checked, notes`）与 `equity_curve`（`run_id, trade_date, nav, bench_nav, drawdown`）。**`repro_hash` = 代码 + 参数 + `data_snapshot_date` 的哈希，保证可复现。**
9. **委托风控复核（必须）**：用 `task` 委托 **`risk-reviewer`** 子智能体复核（防前视/防幸存者偏差/防过拟合/成本与可成交性）。**risk-reviewer 只读复算不写库**，主 Agent 拿到 `verdict` 后用 `quant-db_write_query` 把 `review_id` + `verdict` + `findings_json` 写回 `risk_reviews`，并把 `review_id` 记录回 `backtests.notes`。复核未通过前不得交付"可执行"结论。

## 输出格式

- 使用 `templates/backtest_template.md` 的结构：策略与区间 → 数据快照与可复现信息 → 绩效指标表 → 成本模型 → 信号后移对照 → 风控复核结论 → 局限与声明。
- 指标表逐项给值（无数据显示"未计算"），**不得编造收益率/夏普/回撤**。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `回测_{strategy_id}_{start}_{end}.md`。

## 数据口径（写死，不得违背）

- 代码统一 `code.exchange`；universe **必须保留退市标的**（`delist_date` 非空）。
- 基本面**必须带 `as_of_date`**；信号 `trade_date` 为生成日，**成交必须在下一交易日（T+1）**，回测不许用当日收盘价成交。
- 价格用复权口径，**量（`volume`/`amount`）恒为未复权原始口径**，禁止混用。
- 成本模型必须计入：佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点。
- 交易单位：A股/ETF 100 股（份）；科创板 200 股。

## 禁止事项

- 禁止编造回测收益、夏普、回撤、IC、胜率等任何数值；未计算就写"未计算"。
- 禁止不写 `cost_model_json`、`data_snapshot_date`、`repro_hash`、`param_grid_size`、`lag_shift_checked`——缺一即视为不合格回测。
- 禁止省略"信号后移 1 日"对照，禁止用当日收盘价成交，禁止用 `pub_date > 调仓日` 的财报。
- 禁止剔除退市标的做 universe（幸存者偏差）。
- **禁止跳过 `risk-reviewer` 复核就交付结论**；`risk_reviews.verdict` 为"通过"前不得声称可执行。
- 禁止在本技能产出实盘下单指令；回测结论要执行请转 `signal-review`。

## 多智能体协作

回测闭环后**必须**用 `task` 委托 `risk-reviewer` 复核；该子代理只读复算、不写库，其 `verdict` 决定该结果能否进入执行环节（trade-gateway 下单闸门要求的 `risk_reviews.verdict='通过'` 凭证即来源与此）。
