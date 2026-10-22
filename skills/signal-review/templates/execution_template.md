# 调仓执行计划单（模板）

> 复用本结构生成执行清单。带 `{}` 的为占位说明，正式输出时替换；不适用的小节保留标题并注明"无"。

## 一、信号概览

| 项 | 内容 |
|---|---|
| 信号日期 trade_date | {yyyy-MM-dd（信号生成日）} |
| 计划成交日 | {下一交易日 T+1：yyyy-MM-dd} |
| 策略/范围 | {strategy_id 或"全部"} |
| 待处理信号数 | {N}（status in new/reviewed） |

## 二、可成交性核对

| 代码 | 名称 | 方向 | 停牌 | 一字板 | 涨停 | 跌停 | tradable | 结论 |
|---|---|---|---|---|---|---|---|---|
| {510300.SH} | {沪深300ETF} | {buy} | {否} | {否} | {…} | {…} | {true} | {可成交/剔除/顺延} |

> 剔除或顺延的标的必须写明原因（停牌/退市/一字板/前收缺失）。

## 三、调仓执行清单（不含下单）

| 序 | side | 代码 | 名称 | 计划数量 | lot_size | 参考价 | 预估金额 | 预估成本 | 计划成交日 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | {buy} | {510300.SH} | {沪深300ETF} | {1000} | {100} | {…} | {…} | {…} | {T+1} |

## 四、风控参数核对（对照 strategies.risk_limits_json）

| 维度 | 限额 | 本次计划值 | 结论 |
|---|---|---|---|
| max_position_pct | {0.4} | {…} | {通过/超限} |
| max_order_amount | {100000} | {…} | {…} |
| min_cash_pct | {0.05} | {…} | {…} |
| stop_loss | {0.08} | {…} | {…} |
| allow_leverage | {false} | {…} | {…} |

## 五、风控复核结论

- reviewer：{risk-reviewer}
- review_id：{RR-yyyyMMdd-NN}
- verdict：{通过 / 补充措施后可执行 / 不建议执行}
- findings：{逐条：维度 | 证据 | 判定 | 建议}

## 六、落库与下一步

- 落库：signals.risk_review_id 回填 {review_id}；status → {reviewed}。
- 下一步：如需真正下单，转 live-trading（先 get_trading_mode，再 prepare_order → 用户贴回确认码 → place_order）。
- 声明：本单仅为执行计划，不含下单动作。
