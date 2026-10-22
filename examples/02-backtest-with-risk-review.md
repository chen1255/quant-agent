# 样例 02 · 跑一次 ETF 动量轮动回测并送风控复核

> 这是一份**可复现的操作样例（runbook）**，不是对话记录，也不含任何真实回测收益。
> 所有绩效数字用 `<...>` 占位，必须由你自己跑出来。字段名与工具名取自仓库真实代码。

## 目标

用种子库里已有的 `etf-momentum-20d` 策略，跑一次含成本、可复现、做过信号后移对照的回测，
落 `backtests` + `equity_curve`；然后通过 `task` 委托 `risk-reviewer` 独立复核，把
`verdict` / `findings_json` 写进 `risk_reviews`，最后用 SQL 核对凭证是否真的落库。

## 前置条件

- 已完成 [样例 01](01-data-ops-runbook.md)：4 只 ETF 的日线已入库，`trading_calendar` 可用
- `strategies` 中存在 `etf-momentum-20d`（`seed.sql` 已灌入）；若被删过，用 `quant-db_read_query` 确认
- `risk-reviewer` 已注入到 `config.yaml` 的 `subagents.custom_agents`，且 `allowed_subagents`
  只授权它一个
- 会话开启了 `subagent_enabled`（否则不挂载 `task` 工具）

## 逐步操作

### 1. 读策略定义与风控限额

```sql
SELECT strategy_id, name, universe_json, params_json, risk_limits_json, rebalance_rule
FROM strategies WHERE strategy_id='etf-momentum-20d';
```

应看到：universe 为 `510300.SH / 510500.SH / 159915.SZ / 518880.SH` 四只 ETF，参数为
`momentum_20d`、`top_n=2`、等权、`ma_filter=60`，调仓规则为「每月首个交易日」。
**参数与限额必须取自库，不能凭记忆重写。**

### 2. 固定数据快照日

```sql
SELECT MAX(trade_date) AS snapshot
FROM bars_daily
WHERE symbol IN ('510300.SH','510500.SH','159915.SZ','518880.SH');
```

这个值就是 `backtests.data_snapshot_date`，也是 `repro_hash` 的输入之一。同一次回测的
所有取数都必须止于该日 —— 否则「快照日」就是一句空话。

### 3. 取数并算因子

- 价格用 `quant-data_get_daily_bars(..., adjust="qfq")`；**`volume` / `amount` 恒为未复权原始口径**；
- 财务因子（本例不用）必须 `quant-data_get_fundamentals(symbols, as_of_date=调仓日)`；
- 调仓日与成交日一律由 `quant-data_get_trading_calendar` 决定；
- 可成交性由 `quant-data_get_limit_status(symbols, trade_date)` 判定，`tradable=false` 直接剔除。

因子值落 `factor_values`（`factor_name` / `symbol` / `trade_date` / `raw_value` / `z_score` /
`pct_rank`，主键 `factor_name,symbol,trade_date`，重算用 `ON CONFLICT` 覆盖）。

### 4. 生成信号（信号日 ≠ 成交日）

```sql
SELECT signal_id, trade_date, symbol, side, score, target_weight, status
FROM signals WHERE strategy_id='etf-momentum-20d' ORDER BY trade_date;
```

`trade_date` 是**信号生成日**；成交必须在 `trading_calendar.prev_open` 给出的**下一交易日**，
回测不允许用当日收盘价成交。

### 5. 写 `backtests`（缺一即不合格）

必须写入的字段：`cost_model_json`、`data_snapshot_date`、`repro_hash`、`param_grid_size`、
`lag_shift_checked`。示例（绩效值全部为占位符）：

```sql
INSERT INTO backtests
  (run_id, strategy_id, start_date, end_date, sample_type, cost_model_json, benchmark,
   annual_return, annual_vol, max_drawdown, max_dd_duration, sharpe, calmar,
   win_rate, profit_loss_ratio, turnover, param_grid_size,
   data_snapshot_date, repro_hash, lag_shift_checked, notes)
VALUES
  ('BT-<日期>-001', 'etf-momentum-20d', '<回测起始>', '<回测结束>', 'out',
   '{"commission_rate":0.00025,"min_commission":5.0,"stamp_tax_rate":0.0005,
     "transfer_fee_rate":0.00001,"slippage_rate":0.001}',
   '000300.SH',
   <年化收益率>, <年化波动>, <最大回撤>, <最长回撤持续天数>, <夏普>, <Calmar>,
   <胜率>, <盈亏比>, <年换手率>, <参数网格规模>,
   '<数据快照日>', '<repro_hash>', 1,
   'cost_model 与 gates.estimate_costs 同口径；已做信号后移 1 日对照');
```

同时逐日写 `equity_curve(run_id, trade_date, nav, bench_nav, drawdown)`。

### 6. 委托 `risk-reviewer`（必须）

```
task(
  subagent_type="risk-reviewer",
  prompt="请复核以下回测与拟执行计划。策略 etf-momentum-20d（universe=四只ETF，top_n=2，
          动量20日，ma_filter=60，月频调仓）；回测区间 <起始>~<结束>，sample_type=out；
          cost_model_json=<同上>；data_snapshot_date=<快照日>；repro_hash=<哈希>；
          lag_shift_checked=1；param_grid_size=<网格规模>；绩效=<按上表逐项>。
          拟下单标的与金额：<标的/金额>。请按 11 个维度出具三档结论。"
)
```

`risk-reviewer` 会自己对 `quant-db` 做只读复算（universe 是否含退市标的、`fundamentals`
是否只有 `pub_date<=调仓日` 的记录、`bars_daily` 成交额是否够、`cost_model_json` 是否缺失），
输出固定结构：**风控复核结论（三档置顶）→ 复核证据表 → 关键风险 → 落库要点**。

### 7. 把结论落库（子代理写不了库，必须主 Agent 写）

```sql
INSERT INTO risk_reviews
  (review_id, target_type, target_id, reviewer, verdict, findings_json, lag_shift_test, reviewed_at)
VALUES
  ('RR-<日期>-001', 'backtest', 'BT-<日期>-001', 'risk-reviewer',
   '<通过 | 补充措施后可执行 | 不建议执行>',
   '[{"dimension":"前视偏差","evidence":"<复核引用到的具体记录>","verdict":"<通过/需整改/不通过>","suggestion":"<建议>"}]',
   '<信号后移 1 日的对照结论>',
   strftime('%s','now'));
```

`target_type` 只能是 `strategy` / `backtest` / `signal` / `order`；`verdict` 只能是三档之一
（`risk_reviews.verdict` 有 CHECK 约束）。`review_id` 由复核结果给出（格式
`RR-{日期}-{序号}`），**不得伪造**。

### 8. 核对凭证与可复现信息

```sql
SELECT r.review_id, r.target_type, r.target_id, r.verdict,
       json_array_length(r.findings_json) AS findings_n,
       r.lag_shift_test, r.reviewed_at
FROM risk_reviews r
WHERE r.target_id='BT-<日期>-001';

SELECT run_id, data_snapshot_date, repro_hash, lag_shift_checked, param_grid_size,
       cost_model_json IS NOT NULL AS has_cost
FROM backtests WHERE run_id='BT-<日期>-001';

SELECT COUNT(*) AS nav_days FROM equity_curve WHERE run_id='BT-<日期>-001';
```

## 预期输出形态

- 复核回复以三档结论**置顶**，随后是「维度 | 证据 | 判定 | 修改建议」表格，最后给出建议落库的
  `review_id` / `verdict` / `findings_json`；
- 落库后 `risk_reviews` 多一行，`findings_json` 是合法 JSON 数组；
- `backtests` 一行，`has_cost=1`、`lag_shift_checked=1`、`repro_hash` 非空；
- `equity_curve` 行数等于回测区间的净值天数。

## 验收标准

- `cost_model_json` 非空且与 `gates.estimate_costs` 同口径（佣金 0.00025 / 最低 5 元 /
  卖出印花税 0.0005 / 过户费 0.00001 / 滑点）；
- `data_snapshot_date` = 本次实际使用的数据最新交易日；
- `lag_shift_checked=1`（做过且通过对照）；
- `risk_reviews` 中该 `target_id` 有一行，`verdict` 落在三档内，`findings_json` 覆盖 11 维度；
- `backtests.notes` 中记录了 `review_id`，形成「回测 → 复核 → 凭证」的闭环；
- **只有 `verdict` 为可执行档时，才允许把这份结果交到 `signal-review` 去做执行清单。**

## 常见失败与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 复核判「不建议执行」 | 存在前视、缺成本模型、无样本外或过拟合 | **不得改判**；按 `findings_json` 修策略或参数后重新回测、重新委托 |
| 复核判「补充措施后可执行」 | 有可整改项但非红线 | 落实 `suggestion` 后再考虑进入执行；凭证只能用于它审过的那个目标 |
| `cost_model_json` 缺失直接不通过 | 未计入佣金/印花税/过户费/滑点 | 补上成本模型重跑；不要用「扣费前收益」交付 |
| 未做后移对照被降档 | `lag_shift_checked=0` | 把信号整体后移 1 个交易日重跑，收益由正转负即判不通过 |
| `risk_reviews` 插入失败（CHECK） | `target_type` 或 `verdict` 不在枚举内 | 按三档原文填写，不要写「基本通过」这类自造档位 |
| JSON 字符串插入报错 | `findings_json` 里引号未转义 | 用参数化写入或把内层引号转义；落库后用 `json_valid()` 复核 |
| 复核子代理没有触发 | 技能未放行 `task`，或 `allowed_subagents` 未含 `risk-reviewer` | 检查 `SKILL.md` 的 `allowed-tools` 是否含 `task`；`task` 不是默认内置白名单 |
| 复核结论含中文时工具超时 | Windows 中文系统 GBK/UTF-8 不匹配 | 三个 MCP 都设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` |
