# 样例 01 · 用 data-ops 建标的池并补齐日线

> 这是一份**可复现的操作样例（runbook）**，不是对话记录。命令与字段取自仓库真实代码；
> 返回中的具体数值一律以你本机实际返回为准。下文用 `<...>` 表示需要你替换的内容。

## 目标

把 4 只宽基/商品 ETF 与 1 只股票建成一个可回测的标的池，补齐指定区间的日线，并用 SQL
核对「覆盖度、数据源、复权口径」三件事 —— 确保后续因子与回测不会因为口径混用而失真。

标的池：`510300.SH`、`510500.SH`、`159915.SZ`、`518880.SH`、`600519.SH`。

## 前置条件

- 已运行 `scripts/install.ps1`，`quant.db` 的 15 张表就绪，`quant-data` 的 venv 可导入
  `akshare` / `baostock`
- Gateway 已重启，新会话已选择「量化交易助手」
- 本机可访问上游（akshare 接口偶发抖动属正常）
- 三个 MCP 的 `env` 都含 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`

## 逐步操作

### 1. 确认库就绪（先看表，不要硬拉上游）

```
quant-db_list_tables()
```

预期能看到 `instruments`、`trading_calendar`、`bars_daily`、`fundamentals`、
`factor_values`、`strategies`、`backtests`、`equity_curve`、`signals`、`risk_reviews`、
`order_intents`、`orders`、`trades`、`positions_daily` 等 15 张表。缺表说明未建库，
先回去跑安装器，**不要**继续调上游。

### 2. 归一化标的（名称/裸代码 → `code.exchange`）

```
quant-data_get_instruments(keyword="510300", include_delisted=True, limit=50)
quant-data_get_instruments(keyword="茅台",   include_delisted=True, limit=50)
```

返回 `data` 是标的数组，`delist_date` 非空表示已退市。本步只做解析，不写入。

### 3. 查本地覆盖度（先查再拉）

```sql
SELECT symbol,
       COUNT(*)          AS rows,
       MIN(trade_date)   AS first_day,
       MAX(trade_date)   AS last_day
FROM bars_daily
WHERE symbol IN ('510300.SH','510500.SH','159915.SZ','518880.SH','600519.SH')
GROUP BY symbol
ORDER BY symbol;
```

空结果表示该标的从未入库；有结果则用 `last_day` 与目标区间比较，算出缺口段。

### 4. 补拉行情（工具会自动落 `bars_daily`）

```
quant-data_get_daily_bars(
  symbols="510300.SH,510500.SH,159915.SZ,518880.SH,600519.SH",
  start_date="2020-01-01",
  end_date="<最新交易日>",
  adjust="qfq",
  use_cache=True
)
```

- 单次最多 20 个标的，超了要分批；
- `adjust` 只接受 `none` / `qfq` / `hfq`；
- 任意一个标的取不到不会让整单失败，该 `symbol` 下会给 `error` 字段；
- **补缺口要注意**：`use_cache=True` 时，只要该区间内已有任意缓存行，就整体走缓存、不会逐日补缺。
  因此正确做法是把区间切成「完全无缓存」的日期段分别调用，或对该段传 `use_cache=False`。

### 5. 核对覆盖度、数据源与因子

```sql
SELECT symbol,
       COUNT(*)                                   AS rows,
       MIN(trade_date)                            AS first_day,
       MAX(trade_date)                            AS last_day,
       GROUP_CONCAT(DISTINCT source)              AS sources,
       GROUP_CONCAT(DISTINCT adjust_mode)          AS adjust_modes,
       SUM(CASE WHEN adj_factor IS NULL THEN 1 ELSE 0 END) AS adj_null,
       SUM(CASE WHEN volume   IS NULL THEN 1 ELSE 0 END)   AS vol_null
FROM bars_daily
WHERE symbol IN ('510300.SH','510500.SH','159915.SZ','518880.SH','600519.SH')
GROUP BY symbol
ORDER BY symbol;
```

判读要点：

- `sources` 应主要是 `akshare`（补位时可能混入 `baostock` 口径的因子，以实际返回为准）；
- `adjust_modes` 在库里应统一为 `hfq` —— 服务端落库时把 `adj_factor` 当作**累计复权因子**
  统一存储，返回给调用方时再按请求的 `adjust` 换算；
- `adj_null` / `vol_null` 应尽量为 0；非 0 说明该段数据不完整。

### 6. 验证没有混用复权口径

先取同一段的三种口径：

```
quant-data_get_daily_bars(symbols="510300.SH", start_date="<某段>", end_date="<某段>",
                          adjust="none", use_cache=False)
quant-data_get_daily_bars(symbols="510300.SH", start_date="<某段>", end_date="<某段>",
                          adjust="qfq",  use_cache=False)
```

再逐项核对：

1. **量不随口径变**：两次返回的 `volume` / `amount` 必须**完全一致**；若变了，说明把量也复权了。
2. **价随口径变**：`none` 与 `qfq` 的 `close` 通常在除权日之后出现差异；若无差异且该标的有分红
   送转，检查 `adj_factor` 是否全为 1.0（baostock 登录失败时会退化为 1.0 并在 `warnings` 提示）。
3. **因子单调可解释**：`hfq` 价 = `none` 价 × `adj_factor`；`qfq` 价 = `none` 价 ×
   `adj_factor` / 最新因子。抽查任意一行必须能对上。

```sql
-- 复权因子是否真的取到了（全为 1.0 说明上游因子缺失）
SELECT symbol, MIN(adj_factor), MAX(adj_factor), COUNT(DISTINCT adj_factor)
FROM bars_daily
WHERE symbol IN ('510300.SH','600519.SH')
GROUP BY symbol;
```

### 7. 补齐交易日历与基本面（回测与 T+1 的前提）

```
quant-data_get_trading_calendar(start_date="2020-01-01", end_date="<最新交易日>")
quant-data_get_fundamentals(symbols="600519.SH", as_of_date="<观察日>")
```

日历要落 `trading_calendar(cal_date, exchange, is_open, prev_open, note)`，`prev_open` 是算
T+1 成交与做「信号后移对照」的基础。基本面**必须带 `as_of_date`**，工具只返回
`pub_date <= as_of_date` 的记录，无公告日的记录会被丢弃。

## 预期输出形态

- `quant-data_get_daily_bars` 返回统一成功信封：
  `{"ok": true, "tool": "get_daily_bars", "data": {"<symbol>": [bar, ...]}, "adjust": "qfq",
  "bars_total": <整数>, "warnings": [...], "volume_note": "volume/amount 为未复权原始口径，勿与复权价混用"}`；
- `bar` 字段：`date` / `open` / `high` / `low` / `close` / `volume` / `amount` /
  `turnover_rate` / `adj_factor` / `adjust_mode` / `source`；
- 失败时是 `{"ok": false, "tool": "...", "error": {"code": "...", "message": "...", "hint": "..."}}`。

## 验收标准

- 5 个标的在 `bars_daily` 中都有行，且 `MAX(trade_date)` 覆盖到目标区间末端（或明确记录缺口原因）；
- `source` 可解释，`adjust_mode` 一致，`adj_factor` 非空；
- 同一段的 `none` 与 `qfq` 返回中 `volume` / `amount` 完全一致；
- `trading_calendar` 有对应区间且 `prev_open` 非空；
- 最终回报里写清每个标的的入库区间、行数、口径、数据源与 warnings —— **data-ops 到此为止，
  不做任何涨跌判断**。

## 常见失败与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| `UPSTREAM_TIMEOUT` / `UPSTREAM_FAILED` | 上游抖动或限频；工具内部已超时 25s、重试 2 次 | **不要**在技能层循环重试；记录失败标的、跳过并继续其余，最后汇总 warnings；稍后换时段补 |
| 某个 `symbol` 下只有 `error` | 代码错误、区间内无交易日、或该标的上游无数据 | 先用 `get_instruments` 确认代码与 `asset_type`；ETF 与股票走上游不同接口，混拉会取空 |
| `EMPTY_DB` | 未建库，或 `QUANT_DB_PATH` 指错 | 重跑 `scripts/install.ps1`；检查 MCP `env.QUANT_DB_PATH` 是否为本机绝对路径 |
| `BAD_ARGUMENT`（日期） | 日期不是 `YYYY-MM-DD`，或 `start_date > end_date` | 修正格式与区间 |
| 含中文的结果 60 秒超时 | Windows 中文系统 MCP stdio 按 GBK 写 stdout | 三个 MCP 都加 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，重启 Gateway |
| `adj_factor` 全为 1.0 | baostock 登录失败，工具已退化为 1.0 并在 warnings 提示 | 检查网络/baostock 可用性后重拉；**不要**自行编造复权因子 |
| 补了区间但缺口还在 | `use_cache=True` 命中缓存后不再补该段 | 按缺口切段查询，或对该段传 `use_cache=False` |
