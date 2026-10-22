# 下单单（模板）

> 复用本结构生成下单记录。带 `{}` 的为占位说明，正式输出时替换；不适用的小节保留标题并注明"无"。

## 〇、前置确认（不可省略）

- 交易模式（get_trading_mode）：`mode = {dry_run / live / unavailable}`；`dry_run={true/false}`。
- 风控凭证：`risk_review_id = {RR-...}`，`verdict = {通过}`（非"通过"不得继续）。
- 用户是否明确要求下单：{是（否则转 signal-review）}。

## 一、下单意图

| 项 | 内容 |
|---|---|
| symbol | {510300.SH} |
| side | {buy / sell} |
| qty | {1000（lot_size 整数倍）} |
| price | {4.00（限价单必填）} |
| order_type | {limit（本期市价单未开放）} |
| strategy_id / signal_id | {…} |
| 计划成交日 | {T+1：yyyy-MM-dd} |

## 二、确认码（必读）

- `intent_id`：{INT-...}
- `confirm_token`：**{6 位数字，原文展示}**
- 有效期：{120 秒；expires_at_readable}
- **请把上面 6 位数字贴回以确认下单**；模型不得自行填写、猜测或重复使用。

## 三、预估金额与成本（estimate_costs 同口径）

| 项 | 金额 |
|---|---|
| 名义金额 notional | {…} |
| 佣金 commission（最低 5 元） | {…} |
| 印花税 stamp_tax（仅卖出） | {…} |
| 过户费 transfer_fee | {…} |
| 滑点 slippage | {…} |
| 合计 total | {…} |

## 四、下单结果（place_order）

| 项 | 值 |
|---|---|
| order_id | {ORD-...} |
| status | {dry_run / submitted / filled / failed} |
| is_dry_run | {true/false} |
| xt_order_id | {…} |
| next_step | {dry_run 演练完成 / 跟踪委托与成交} |

## 五、闸门拒绝记录（如发生）

| 拒绝码 | message | hint | 处理 |
|---|---|---|---|
| {RISK_REVIEW_REQUIRED} | {…} | {…} | {补复核后重新 prepare_order} |

## 六、跟踪与撤单

- 委托：{get_orders 结果}
- 成交：{get_trades 结果}
- 持仓：{get_positions 结果}
- 撤单：{cancel_order 结果 或"未撤单"}

## 七、免责

本项目不构成投资建议，实盘风险由用户自担。
