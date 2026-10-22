# 风控参数单（模板）

> 复用本结构生成风控参数建议。带 `{}` 的为占位说明，正式输出时替换；不适用的小节保留标题并注明"无"。

## 一、策略与账户

| 项 | 内容 |
|---|---|
| strategy_id | {etf-momentum-20d} |
| 策略类型 | {ETF 轮动 / 股票多因子} |
| 账户规模 | {总资产：{…} 元} |
| 当前持仓概览 | {positions_daily 最新快照要点} |

## 二、现状参数（strategies.risk_limits_json）

| 字段 | 当前值 |
|---|---|
| max_position_pct | {0.4} |
| max_order_amount | {100000} |
| stop_loss | {0.08} |
| min_cash_pct | {0.05} |
| allow_leverage | {false} |

## 三、建议参数

| 字段 | 当前值 | 建议值 | 依据 | 生效点（闸门） |
|---|---|---|---|---|
| max_position_pct | {…} | {…} | {集中度/波动} | {check_position_cap} |
| max_order_amount | {…} | {…} | {流动性/单笔冲击} | {check_limits} |
| stop_loss | {…} | {…} | {回撤容忍} | {策略层} |
| min_cash_pct | {…} | {…} | {T+1 现金需求} | {策略层} |
| allow_leverage | {false} | {false} | {红线} | {—} |

## 四、集中度检查

| 维度 | 当前值 | 限额 | 结论 |
|---|---|---|---|
| 单标的最大的权重 | {…} | {max_position_pct} | {通过/超限} |
| 申万一级行业集中度 | {…} | {建议上限} | {…} |
| 前 5 权重合计 | {…} | {建议上限} | {…} |

## 五、流动性检查

| 代码 | 日均成交额(amount) | 拟下单金额 | 占比 | 结论 |
|---|---|---|---|---|
| {510300.SH} | {…} | {…} | {…} | {可接受/冲击偏大} |

> 剔除 ST 与低流动性标的。

## 六、与环境变量的关系

| 策略参数 | 对应环境变量 | 默认值 | 闸门取值规则 |
|---|---|---|---|
| max_order_amount | QUANT_MAX_ORDER_AMOUNT | {100000} | {取更严者} |
| max_position_pct | QUANT_MAX_POSITION_PCT | {0.30} | {取更严者} |

## 七、落库与红线

- 落库语句目标：UPDATE strategies SET risk_limits_json = {…} WHERE strategy_id = {…}；状态：{已确认/待确认}。
- 红线：不放宽风控、不加杠杆、不改环境变量绕开闸门；数据缺失取保守值。
- 独立复核：如需对策略结果复核，另行用 task 委托 risk-reviewer（本单不含）。
