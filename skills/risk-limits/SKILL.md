---
name: risk-limits
description: 风控参数与限额的设定与维护。当用户说"仓位上限多少合适""止损/风控阈值怎么定""限额""单笔最多买多少""现金留多少""帮我改下策略的风控参数"时使用。只维护 `strategies.risk_limits_json` 等风控参数，并给出集中度与流动性口径。要针对某个策略结果做独立复核改用 risk-reviewer（经 task 委托）；要回测改用 strategy-backtest；要下单改用 live-trading。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - task
argument-hint: "策略（strategy_id）+ 想设定/调整的风控项（仓位上限/单笔上限/止损/现金/杠杆）"
---

# 风控限额技能（risk-limits）

把"风控参数该怎么定"收敛成**有依据、可落库、不放宽红线**的参数集合。
本技能只维护风控参数，**不评估策略历史收益、不下单、不给放宽风控的捷径**。

## 触发逻辑

- **触发方式**：为某个策略设定或调整风控参数（限额/止损/现金/杠杆），或解释这些参数的口径。
- **触发示例**：
  - "这个策略的仓位上限应该多少"
  - "止损设 8% 还是 15%"
  - "单笔最多能买多少钱"
  - "帮我改一下 risk_limits_json，现金至少留 5%"
- **不触发**：要针对某个策略回测结果做**独立复核**（防前视/过拟合/成本）→ 用 `task` 委托 `risk-reviewer`；要跑回测 → `strategy-backtest`；要下实盘单 → `live-trading`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| strategy_id | 风控参数按策略存于 `strategies.risk_limits_json` |
| 要设定/调整的项 | `max_position_pct`/`max_order_amount`/`stop_loss`/`min_cash_pct`/`allow_leverage` |
| 账户规模与策略类型 | 决定限额量级（单笔上限、现金留存） |

## 工作流程（严格按序，不跳步）

1. **读现状**：`quant-db_list_tables` / `quant-db_describe_table` 确认表结构；`quant-db_read_query` 读 `strategies` 里目标策略的 `risk_limits_json`、`universe_json`、`params_json`，以及 `positions_daily` 的最新持仓分布。
2. **核对口径与量级**：明确各项含义——
   - `max_position_pct`：单标的最大仓位占比（对照环境变量 `QUANT_MAX_POSITION_PCT`，默认 0.30）；
   - `max_order_amount`：单笔金额上限（对照 `QUANT_MAX_ORDER_AMOUNT`，默认 100000）；
   - `stop_loss`：止损线（小数，如 0.08）；
   - `min_cash_pct`：最低现金留存比例；
   - `allow_leverage`：是否允许杠杆（默认 `false`）。
   **实际下单闸门取"策略参数与环境变量中的更严者"**：即使策略文件写宽了，MCP 层仍按 `QUANT_MAX_ORDER_AMOUNT` / `QUANT_MAX_POSITION_PCT` 拦截。
3. **集中度检查**：给出单标的权重、**申万一级行业**（`instruments.industry_sw1`）集中度、前 5 权重合计三项，并对照限额给出建议区间。行业/个股过度集中要收窄而非放宽。
4. **流动性检查**：用 `bars_daily.amount` 估标的日均成交额，与拟下单金额对比，给出"单笔不应超过日均成交额一定比例"的约束（冲击成本）；剔除 ST 与低流动性标的。
5. **给出参数建议**：以保守为先，输出建议值 + 依据 + 失效条件；说明每项参数在下单闸门（`gates.check_limits` / `check_position_cap`）中的作用。
6. **落库**：经用户确认后用 `quant-db_write_query` 更新 `strategies.risk_limits_json`（JSON 字符串），保持字段名与既有格式一致。
7. **独立复核（如需）**：对某个策略**结果**的复核不属于本技能，用 `task` 委托 **`risk-reviewer`** 子智能体；本技能只负责参数本身。

## 输出格式

- 使用 `templates/risk_limits_template.md` 的结构：策略与账户 → 现状参数 → 建议参数表 → 集中度 → 流动性 → 与环境变量关系 → 落库与红线。
- 参数表逐项给：字段 | 当前值 | 建议值 | 依据 | 生效点（闸门）。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `风控参数_{strategy_id}_{yyyyMMdd}.md`。

## 数据口径（写死，不得违背）

- 金额单位为人民币元；比率类字段一律小数（`0.08` 表示 8%），`risk_limits_json` 存 JSON 字符串。
- 交易单位：A股/ETF 100 股（份）；科创板 `688` 开头 200 股。
- 成本口径：佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点。

## 红线

- **不给放宽风控或加杠杆的捷径**：不主动建议提高 `max_position_pct`/`max_order_amount`、不解除 `min_cash_pct`、不打开 `allow_leverage`；用户若要求放宽，明确风险并建议保守档。
- 不修改环境变量 `QUANT_MAX_ORDER_AMOUNT` / `QUANT_MAX_POSITION_PCT` 以绕开闸门。
- 数据缺失时按最保守判，不给模糊放行。

## 禁止事项

- 禁止编造回测收益、波动率或成交额来"证明"某个限额合理；无数据就写"数据不足，取保守值"。
- 禁止替代 `risk-reviewer` 对策略结果下复核结论；本技能只管参数。
- 禁止在本技能下单或生成下单指令。
- 禁止把集中度/流动性检查做成了摆设：任何收窄建议都要有对齐的限额值。
