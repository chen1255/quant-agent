# 回测报告单（模板）

> 复用本结构生成回测报告。带 `{}` 的为占位说明，正式输出时替换；不适用的小节保留标题并注明"无"。

## 一、策略与区间

| 项 | 内容 |
|---|---|
| strategy_id | {etf-momentum-20d} |
| universe | {asset_types / index_code / symbols；含退市标的：是} |
| 因子 | {factor_name + 参数} |
| 调仓规则 | {如"每月首个交易日"} |
| start_date / end_date | {yyyy-MM-dd ~ yyyy-MM-dd} |
| sample_type | {in / out / walk_forward} |
| benchmark | {000300.SH} |

## 二、数据快照与可复现信息

| 项 | 值 |
|---|---|
| data_snapshot_date | {yyyy-MM-dd} |
| repro_hash | {hash（代码+参数+数据快照）} |
| param_grid_size | {N} |
| lag_shift_checked | {1=已做且通过对照 / 0=未做} |

## 三、绩效指标

| 指标 | 值 | 指标 | 值 |
|---|---|---|---|
| 年化收益 | {年化收益率} | 年化波动 | {…} |
| 最大回撤 | {…} | 回撤持续期(天) | {…} |
| Sharpe | {…} | Calmar | {…} |
| 胜率 | {…} | 盈亏比 | {…} |
| 年换手(倍) | {…} |  |  |

> 未计算的指标写"未计算"，不得编造。

## 四、成本模型（必须计入）

| 项 | 取值 |
|---|---|
| 佣金 | {万 2.5，最低 5 元} |
| 印花税（仅卖出） | {0.05%} |
| 过户费 | {0.001%} |
| 滑点 | {…} |
| cost_model_json | {完整 JSON 或"缺失（不合格）"} |

## 五、信号后移对照

- 对照设置：{信号整体后移 1 个交易日重跑}
- 对照结果：{收益是否由正转负 / 结论}
- 结论：{lag_shift_checked = 1/0}

## 六、风控复核结论

- reviewer：{risk-reviewer}
- review_id：{RR-yyyyMMdd-NN}
- verdict：{通过 / 补充措施后可执行 / 不建议执行}
- 关键 findings：{逐条：维度 | 证据 | 判定 | 建议}

## 七、局限与声明

- 指数成分口径：{point_in_time=false 声明，如适用}
- 数据 warnings：{逐条；无则写"无"}
- 交易单位与 T+1：{A股/ETF 100 股、科创板 200；成交在下一交易日}
- 声明：本报告为回测结论，不产生实盘指令；执行须经 signal-review 与 live-trading。
