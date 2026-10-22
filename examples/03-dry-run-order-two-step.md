# 样例 03 · dry_run 下单的两步走

> 这是一份**可复现的操作样例（runbook）**，不是对话记录。本文不包含任何真实资金账号、
> 真实委托编号或真实报单记录；所有账号/单号均为占位符。

## 目标

在 `dry_run` 模式下走完「生成下单意图 → 用户贴回 6 位确认码 → 下单」的两步流程，验证：
① 没有风控凭证就不可能拿到确认码；② 确认码 2 分钟过期且只能消费一次；③ `dry_run` 只写本地
`orders` 表、`xt_order_id` 为空、券商侧无任何委托。

## 前置条件

- `trade-gateway` 已在 `extensions_config.json` 中启用（`install.ps1 -EnableTrading`），且
  `QUANT_TRADE_DRY_RUN=1`（默认值，不要改成 0）
- `quant.db` 中 `risk_reviews` 已有一条**可执行档**记录（`verdict` 为 `通过` 或
  `补充措施后可执行`），且它审的目标与本次下单一致
- `bars_daily` 中有该标的的最近行情（`prepare_order` 用最近两根 bar 推算前收与涨跌停）
- `instruments` 中有该标的的 `lot_size` / `board` / `is_st` / `delist_date`
- 用户**明确**下达了交易指令（纯研究意图应转 `signal-review`，不得进入本流程）

## 逐步操作

### 1. 先看交易模式（未看模式不得继续）

```
trade-gateway_get_trading_mode()
```

预期形态：

```json
{"ok": true, "tool": "get_trading_mode",
 "data": {"mode": "dry_run", "dry_run": true,
          "qmt_path": null, "account": "<你的资金账号或 null>",
          "account_type": "stock", "positions_available": false,
          "gates": {"gate1_default_off": true,
                    "gate2_risk_review": "需 risk_reviews.verdict='通过'",
                    "gate3_confirm_token": "6 位数字，120s TTL，单次消费",
                    "gate4_precheck": "单笔上限 100,000 元，单标的上限 30%"},
          "stats": {"passed_reviews": <整数>, "pending_intents": <整数>}},
 "warnings": ["QUANT_TRADE_DRY_RUN=1：下单只写本地 orders 表，不会报给券商"]}
```

`mode` 为 `unavailable` 时仍可用 `prepare_order` / `place_order` 走 dry_run 通路演练全流程；
只有 `live` 才会真报单。

### 2. 确认风控凭证真实存在

```sql
SELECT review_id, target_type, target_id, verdict, reviewer, reviewed_at
FROM risk_reviews
WHERE verdict IN ('通过','补充措施后可执行')
ORDER BY reviewed_at DESC LIMIT 20;
```

记下 `<review_id>` 与它审过的 `<target_id>`。**不要手写一个 review_id** —— 闸门会去表里查，
查不到就是 `RISK_REVIEW_NOT_FOUND`。

### 3. 生成意图与确认码

```
trade-gateway_prepare_order(
  symbol="510300.SH",
  side="buy",
  qty=100,
  price=<用户确认的限价>,
  order_type="limit",
  risk_review_id="<上一步查到的 review_id>",
  strategy_id="etf-momentum-20d",
  signal_id="<可选，关联信号>"
)
```

`qty` 必须是 `instruments.lot_size` 的整数倍（A股/ETF 100，科创板 200）；
`est_amount = qty × price` 不得超过 `QUANT_MAX_ORDER_AMOUNT`（默认 100000）。
传入 `signal_id` 时，服务端会把该信号的 `risk_review_id` 回填、`status` 置为 `approved`。

预期返回（确认码为占位，实际是随机 6 位数字）：

```json
{"ok": true, "tool": "prepare_order", "dry_run": true,
 "data": {"intent_id": "INT-<时间戳>-<8位HEX>",
          "confirm_token": "<6 位数字>",
          "expires_at": <Unix 秒>, "expires_at_readable": "<YYYY-MM-DD HH:MM:SS>",
          "ttl_seconds": 120,
          "risk_review": {"review_id": "<review_id>", "verdict": "<通过>", "reviewer": "risk-reviewer"},
          "order": {"symbol": "510300.SH", "side": "buy", "qty": 100,
                    "price": <限价>, "order_type": "limit",
                    "est_amount": <金额>, "lot_size": 100,
                    "limit_up": <涨停价>, "limit_down": <跌停价>},
          "estimated_costs": {"notional": <金额>, "commission": <佣金>,
                              "stamp_tax": 0.0, "transfer_fee": <过户费>,
                              "slippage": <滑点>, "total": <合计>}},
 "warnings": ["确认码 2 分钟内有效、只能使用一次；请把原文交给用户确认后贴回，不要由模型自行推断或重复使用"]}
```

### 4. 把确认码原文交给用户，等用户贴回

这一步是**人工闸门**：模型必须把 6 位数字原样展示，并明确请求用户确认。模型不得自行填写、
不得猜测、不得对过期或错误的码循环重试。用户确认后，由用户把数字贴回对话。

### 5. 下单（第二步）

```
trade-gateway_place_order(intent_id="INT-<...>", confirm_token="<用户贴回的 6 位数字>")
```

预期形态：

```json
{"ok": true, "tool": "place_order", "dry_run": true,
 "data": {"order_id": "ORD-<时间戳>-<8位>", "intent_id": "INT-<...>",
          "status": "dry_run", "is_dry_run": true, "xt_order_id": null,
          "symbol": "510300.SH", "side": "buy", "qty": 100, "price": <限价>,
          "est_amount": <金额>, "estimated_costs": {...},
          "next_step": "dry_run 演练完成：orders 表已记录，券商无报单"},
 "warnings": ["dry_run=1：仅写本地 orders 表，未向券商报单"]}
```

### 6. 用 SQL 核对（不要只信工具回显）

```sql
-- 订单：dry_run 且无券商委托号
SELECT order_id, intent_id, symbol, side, qty, price, fee, tax, transfer_fee,
       status, is_dry_run, xt_order_id, submitted_at
FROM orders WHERE order_id='ORD-<...>';

-- 意图：已消费、有消费时刻
SELECT intent_id, status, confirm_token, expires_at, consumed_at, risk_review_id
FROM order_intents WHERE intent_id='INT-<...>';
```

### 7. 跟踪与撤单

```
trade-gateway_get_orders(limit=20)
trade-gateway_get_trades(limit=50)
trade-gateway_get_positions()
trade-gateway_cancel_order(order_id="ORD-<...>")
```

`get_positions` 在 dry_run / xtquant 不可用时回退到 `positions_daily` 日终快照，不是实时持仓。
撤单默认只允许撤卖单（`QUANT_CANCEL_SELL_ONLY=1`）；`filled` / `cancelled` / `rejected` /
`failed` 状态一律不可撤。

## 会被 MCP 层拒绝的情况

| 拒绝码 | 触发条件 | 处置 |
|---|---|---|
| `RISK_REVIEW_REQUIRED` | 没传 `risk_review_id`，或生成确认码前缺凭证 | 先走 `task(risk-reviewer)` 复核并落库 |
| `RISK_REVIEW_NOT_FOUND` | `review_id` 在 `risk_reviews` 中不存在 | 用真实落库的 id，不要伪造 |
| `RISK_REVIEW_REJECTED` | `verdict` 不在可执行档（如「不建议执行」） | 按 findings 整改后重新复核 |
| `RISK_REVIEW_TARGET_MISMATCH` | 凭证指向的 `target_id` 与本次下单目标不一致 | 一纸凭证只能用于它审过的目标 |
| `INTENT_NOT_FOUND` | `intent_id` 不存在 | 重新 `prepare_order` |
| `INTENT_EXPIRED` | 超过 120 秒 TTL | 重新 `prepare_order` 拿新码 |
| `INTENT_ALREADY_CONSUMED` | 同一确认码被第二次使用（或并发消费） | 重新 `prepare_order`；不要重试旧码 |
| `TOKEN_MISMATCH` | 6 位数字不正确 | 向用户索取正确数字；猜错**不会**消耗 token，可重试正确码 |
| `BAD_ARGUMENT` | token 非 6 位数字，或 `intent_id` / `confirm_token` 为空 | 按格式补全 |
| `ORDER_AMOUNT_EXCEEDED` | 单笔金额超过 `QUANT_MAX_ORDER_AMOUNT` | 拆单或调低数量（改限额需人工确认） |
| `LOT_SIZE_VIOLATION` | 数量不是 `lot_size` 整数倍 | A股/ETF 取 100 的倍数，科创板取 200 的倍数 |
| `SYMBOL_SUSPENDED` | 标的停牌或已退市 | 剔除或顺延 |
| `PRICE_ABOVE_LIMIT_UP` / `PRICE_BELOW_LIMIT_DOWN` | 买价高于涨停 / 卖价低于跌停 | 调整限价到区间内 |
| `MARKET_ORDER_DISABLED` | 传了 `order_type="market"` | 本期只支持限价单 |
| `TRADER_UNAVAILABLE` / `DB_UNAVAILABLE` | QMT 不可用 / 库不可用 | 检查 QMT 登录与 `QUANT_DB_PATH`；dry_run 通路的 `prepare_order` 仍可用 |

拒绝时返回的是结构化信封：`{"ok": false, "tool": "...", "blocked_by_gate": true,
"error": {"code": "...", "message": "...", "hint": "..."}}`。主助手应如实转述，**不得换路绕过**。

## 验收标准

- `orders` 中该行 `status='dry_run'`、`is_dry_run=1`、`xt_order_id IS NULL`；
- `order_intents` 中该 `intent_id` 的 `status='consumed'` 且 `consumed_at` 非空；
- `orders.risk_review_id` 链路可追溯（`intent_id → order_intents.risk_review_id`）；
- 券商侧没有任何委托（dry_run 的定义就是「只写库」）；
- 用旧确认码再调一次 `place_order` 必须得到 `INTENT_ALREADY_CONSUMED`，而不是第二次下单成功。

## 常见失败与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| `prepare_order` 直接拒绝，连码都没给 | 缺/无效/非可执行档的风控凭证 | 这是闸 2 正常工作；补齐凭证，不要试图绕过 |
| 用户贴回时已超过 2 分钟 | TTL 到期，意图被置 `expired` | 重新 `prepare_order`，让用户再确认一次 |
| 模型试图自己填确认码 | 违反 `live-trading` 的五步铁律 | 必须停下并向用户索取；任何猜测都会 `TOKEN_MISMATCH` |
| `est_amount` 超限 | 数量 × 价格超过 `QUANT_MAX_ORDER_AMOUNT` | 缩小 `qty` 或拆单；不得自行改环境变量 |
| 涨停价/跌停价取不到 | `bars_daily` 缺前收或最近行情 | 先用 data-ops 补数；不可判定即不可交易 |
| `get_positions` 返回空 | dry_run 或无 QMT，且 `positions_daily` 无快照 | 这是预期降级，不是实时持仓；不要据此推断账户状态 |
| 中文拒绝信息超时 | Windows 中文系统 GBK/UTF-8 不匹配 | 三个 MCP 都设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` |
