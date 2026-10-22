---
name: signal-review
description: 信号复核与调仓执行计划编排。当用户说"今天有什么信号""要不要调仓""出一份执行计划""这批信号怎么处理""看看现在的持仓和信号"时使用。读取 `signals`，核对可成交性（停牌/涨跌停/T+1/最小单位），形成调仓执行清单并委托 `risk-reviewer` 核对，**但不含下单**。要真正买入/卖出/下单/撤单/查实时持仓改用 live-trading；要评估策略历史收益改用 strategy-backtest；要建候选池改用 factor-screening。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - quant-data_get_instruments
  - quant-data_get_daily_bars
  - quant-data_get_limit_status
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - task
argument-hint: "信号日期（如今天）+ 策略/信号范围 + 目标仓位或可用资金"
---

# 信号复核技能（signal-review）

把"今天该不该调仓"收敛成**可直接执行、经风控核对**的调仓清单。
本技能产出执行计划，**不含下单动作**。

## 触发逻辑

- **触发方式**：查看待处理信号、核对可成交性、编排调仓执行清单。
- **触发示例**：
  - "今天有什么信号"
  - "这批信号要不要调仓，给我个执行计划"
  - "看看现在持仓和最新信号，怎么调整"
  - "把要买的、要卖的列个清单，核对下能不能成交"
- **不触发**：要实际下单/撤单/查实时持仓 → `live-trading`；要评估策略历史收益 → `strategy-backtest`；要建候选池 → `factor-screening`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| 信号日期 | 决定读哪天的 `signals`（`trade_date` 为信号生成日） |
| 策略/信号范围 | 全部还是某一个 `strategy_id`；影响清单范围 |
| 目标仓位或可用资金 | 把权重换算成数量必须有钱与仓位口径 |

## 工作流程（严格按序，不跳步）

1. **读信号**：用 `quant-db_read_query` 读 `signals` 中 `status IN ('new','reviewed')` 的记录，按 `trade_date` 倒序；同时读 `strategies.risk_limits_json`、`positions_daily`（最新快照）与账户可用资金口径。
2. **确认标的属性**：`quant-data_get_instruments`（`include_delisted=True`）拿 `board`/`lot_size`/`is_st`/`delist_date`；退市/停牌标的必须剔除。
3. **核对可成交性（关键）**：`quant-data_get_limit_status(symbols, trade_date)` 取涨跌停价、`is_suspended`、`is_one_word_board`、`tradable`。**`tradable=false` 的标的必须剔除或顺延**——不可判定即不可交易，这是保守方向的降级。
4. **确认 T+1 成交日**：信号 `trade_date` 是生成日，**成交必须在下一交易日**；执行清单里显式写出计划成交日。
5. **数量换算**：按目标权重/可用资金换算数量，取整到最小交易单位——A股/ETF 100 股（份），科创板 `688` 开头 200 股。
6. **核对风控参数**：对照 `strategies.risk_limits_json`（`max_position_pct` / `max_order_amount` / `stop_loss` / `min_cash_pct` / `allow_leverage`）逐条核对单标的仓位、单笔金额、现金留存。
7. **委托风控复核（必须）**：用 `task` 委托 **`risk-reviewer`** 子智能体复核这份执行计划（前视/集中度/流动性/可成交性/成本）。risk-reviewer 只读复算不写库，主 Agent 用 `quant-db_write_query` 把 `review_id` + `verdict` + `findings_json` 写回 `risk_reviews`，并把 `signals.risk_review_id` 回填、`status` 置为 `reviewed`（复核通过后再由 live-trading 置 `approved`）。
8. **输出执行清单**：逐条给出（买/卖、代码、计划数量、参考价、计划成交日、预估金额与成本、可成交性结论），**明确本技能止于清单，不下单**。

## 输出格式

- 使用 `templates/execution_template.md` 的结构：信号概览 → 可成交性核对表 → 调仓执行清单 → 风控参数核对 → 风控复核结论 → 下一步（转 live-trading）。
- 执行清单用表格呈现，每个标的给出可成交性判定与剔除原因。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `执行计划_{日期}.md`。

## 数据口径（写死，不得违背）

- 代码统一 `code.exchange`；`signals.trade_date` 为信号生成日，**成交必须在下一交易日（T+1）**。
- 数量取整到最小交易单位：A股/ETF 100 股（份）；科创板 200 股。
- 停牌、退市、一字板、`tradable=false` 一律不作为可成交标的。
- 成本口径与回测一致：佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点。

## 禁止事项

- 禁止在本技能下单、报单、撤单；**本技能不含任何下单动作**，需要执行请转 `live-trading`。
- 禁止把停牌/一字板/退市标的放进可成交清单；禁止忽略 `tradable=false`。
- 禁止用信号当日成交口径；禁止编造参考价、金额或成交概率。
- 禁止跳过 `risk-reviewer` 复核直接给出"可执行"结论。
- 禁止擅改 `strategies.risk_limits_json` 或放宽风控；需要调整请转 `risk-limits`。
