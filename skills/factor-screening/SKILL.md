---
name: factor-screening
description: 用量化因子做选股/选 ETF、打分排名与轮动候选池构建。当用户说"筛选""打分排名""ETF 轮动""帮我按因子选股""动量/波动率/估值筛选""选出前 N 名"时使用。产出候选池并落 `factor_values`（必要时落 `signals`），只做横截面打分与排序，不评估策略历史收益。要回测历史年化/夏普/回撤改用 strategy-backtest；要研究某行业或标的的基本面改用 market-research；要把候选变成可执行清单改用 signal-review。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - web_search
  - web_fetch
  - quant-data_get_instruments
  - quant-data_get_daily_bars
  - quant-data_get_fundamentals
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - task
argument-hint: "universe（如沪深300成分/一组ETF）+ 因子（如 momentum_20d）+ 排序方向与取前 N"
---

# 因子选股技能（factor-screening）

把"选哪些标的"收敛成**口径一致、可复算、可回溯**的横截面打分结果。
本技能只产出候选池与因子值，**不评估策略历史收益、不生成执行计划**。

## 触发逻辑

- **触发方式**：给定 universe 与因子，做横截面计算、打分、排序、取前 N，形成候选池。
- **触发示例**：
  - "在 510300/510500/159915/518880 里按 20 日动量选前 2"
  - "用 pe_ttm 和 roe_ttm 给沪深300成分股打分排名"
  - "帮我做 ETF 轮动筛选，动量最高的持有"
  - "筛出波动率最低的 10 只 ETF"
- **不触发**：要回测这套选股的历史收益/年化/夏普/回撤 → `strategy-backtest`；要做行业/单标的基本面研究 → `market-research`；要把候选变成调仓执行清单 → `signal-review`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| universe | 成分范围决定排序样本；含退市标的会影响结论口径 |
| 因子与方向 | 决定用 `factor_definitions` 里的哪个因子、`higher_better`/`lower_better` |
| 取前 N / 权重方式 | 候选池大小与等权/加权；影响后续 `target_weight` |
| as_of 日期 | 财务类因子须用 `pub_date <= as_of` 的最新一期 |

## 工作流程（严格按序，不跳步）

1. **定 universe**：用 `quant-data_get_instruments`（`include_delisted=True`）与 `quant-data_get_index_members`（若按指数）构建成分。**必须保留退市标的**，否则犯幸存者偏差；使用指数成分时声明 `point_in_time=false`。
2. **查因子定义**：用 `quant-db_read_query` 读 `factor_definitions`，确认 `factor_name` 存在、`direction`、`params_json`、`lookback_days`（校验样本是否够）。不要凭记忆编造因子公式。
3. **取数据**：价格类因子用 `quant-data_get_daily_bars(symbols, start, end, adjust="qfq")`（**量 `volume`/`amount` 恒为未复权原始口径，禁止与复权价交叉**）；财务类因子用 `quant-data_get_fundamentals(symbols, as_of_date=as_of)`，**只用 `pub_date <= as_of` 的记录**。
4. **算因子原值**：按 `factor_definitions.formula` 计算 `raw_value`；样本长度不足 `lookback_days` 的标的剔除并记录。
5. **横截面标准化**：同一 `trade_date` 内做 z-score（`z_score`）与分位（`pct_rank`，0~1）；多因子按 `direction` 统一方向后合成（如 `z_score_mean`）。
6. **落库 `factor_values`**：写 `factor_name, symbol, trade_date, raw_value, z_score, pct_rank`（主键 `factor_name,symbol,trade_date`；`ON CONFLICT` 覆盖重算）。
7. **产出候选池**：按合成分数排序取前 N，给出 `target_weight`（等权或指定权重）。若用户需要形成信号，则落 `signals`（`signal_id, strategy_id, run_id, trade_date, symbol, side, score, target_weight, ref_price, status='new'`）——**`trade_date` 是信号生成日，成交必须在下一交易日（T+1）**。
8. **回报候选与局限**：给出候选表、因子口径、样本剔除情况与 warnings。**本技能不评策略历史，止步于候选池。**

## 输出格式

- 使用 `templates/screening_template.md` 的结构：universe 与因子 → 计算口径 → 因子值/排名表 → 候选池 → 落库记录 → 局限声明。
- 排名表给出：排名 | 代码 | 名称 | 因子原值 | z_score | pct_rank | 合成分 | 入池。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `筛选_{因子}_{as_of}.md`。

## 数据口径（写死，不得违背）

- 代码统一 `code.exchange`；universe 构建**必须保留 `delist_date` 非空的退市标的**。
- 价格用复权口径（`qfq`/`hfq`），**量（`volume`/`amount`）永远是未复权原始口径**，禁止混用。
- 财务因子**必须带 `as_of_date`**，只能用 `pub_date <= as_of` 的财报（防前视）。
- 横截面标准化只在同一 `trade_date` 内进行。
- 信号 `trade_date` 为信号生成日，**成交必须在下一交易日（T+1）**。

## 禁止事项

- 禁止编造因子公式、IC 值、净值或排名；样本不足就如实剔除并说明。
- 禁止用 `pub_date > as_of` 的财务数据（前视）；禁止用当前成分股冒充历史成分。
- 禁止把"前复权价 × 不复权量"混用。
- 禁止在本技能内评估策略历史收益（那是 `strategy-backtest` 的职责），禁止产出下单指令。
- 禁止堆砌大量因子做无依据的"组合优化"；因子数量与样本长度要匹配。
