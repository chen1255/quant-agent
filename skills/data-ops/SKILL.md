---
name: data-ops
description: A股/ETF 行情与基础数据的取数、补齐与入库。当用户说"更新行情""补齐数据""下载日线""加自选""建标的池""把数据入库""刷新标的台账""先取一下数据"时使用。按"查本地覆盖度→补拉上游→落 quant-db→回报口径"收敛，只负责把数据搬进库，不做任何分析判断。要分析行业/标的或评估值不值得买改用 market-research；要选股筛选打分改用 factor-screening；要回测历史收益改用 strategy-backtest。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - web_search
  - web_fetch
  - quant-data_get_trading_calendar
  - quant-data_get_instruments
  - quant-data_get_daily_bars
  - quant-data_get_fundamentals
  - quant-data_get_limit_status
  - quant-data_get_index_members
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - quant-db_write_query
  - task
argument-hint: "标的（代码或名称，如 600519.SH/510300）+ 起止日期 + 复权口径（none/qfq/hfq）"
---

# 数据运维技能（data-ops）

把"缺数据"这件事收敛成**可复现、口径一致、可审计**的入库动作。
本技能只做取数与入库，**不做任何投资判断、不产出信号、不下单**。

## 触发逻辑

- **触发方式**：需要把行情/标的台账/基本面/交易日历从上游取回并写入 quant-db。
- **触发示例**：
  - "把 510300 和 600519 的日线更新到最新"
  - "补齐 159915.SZ 今年以来的行情"
  - "刷新一下标的台账，把退市的也带上"
  - "给这个策略建个标的池，先取数入库"
  - "交易日历入库，方便后面算 T+1"
- **不触发**：要分析行业/标的、研究值不值得买 → `market-research`；要筛选打分/ETF 轮动 → `factor-screening`；要回测历史收益 → `strategy-backtest`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**最关键的问题：

| 信息 | 为什么关键 |
|---|---|
| 标的（代码或名称） | 代码须统一为 `code.exchange`；只有名称时先 `get_instruments` 解析 |
| 起止日期 | 决定补拉区间；不明时以本地最新日期为起点、今天为终点 |
| 复权口径（none/qfq/hfq） | 口径不同价格不可比；量永远是未复权原始口径 |
| asset_type（stock/etf/index） | ETF 与股票走上游不同接口，混拉会取空 |

## 工作流程（严格按序，不跳步）

1. **确认库就绪**：先 `quant-db_list_tables` 看关键表是否齐全（`instruments` / `bars_daily` / `fundamentals` / `trading_calendar`），缺表说明未建库，提示运行 `scripts/install.ps1`，不要硬拉上游。
2. **归一化标的**：所有代码统一 `code.exchange`，如 `600519.SH`、`510300.SH`、`159915.SZ`。名称或裸代码先用 `quant-data_get_instruments`（`include_delisted=True`）解析成规范代码；解析不到就回报给用户。
3. **查本地覆盖度（先查再拉）**：用 `quant-db_read_query` 查 `bars_daily` 里该标的的 `MAX(trade_date)` 与行数，算出缺口区间；**已覆盖的日期段不重复拉**，避免上游限频。
4. **补拉行情**：对缺口调用 `quant-data_get_daily_bars(symbols, start_date, end_date, adjust, use_cache=True)`。单次最多 20 个标的，超了分批；该工具会把新拉到的行**自动落 `bars_daily`**。注意参数 `adjust` 只取 `none`/`qfq`/`hfq`。
5. **交易日历入库**：用 `quant-data_get_trading_calendar(start_date, end_date)` 取日历，写 `trading_calendar(cal_date, exchange, is_open, prev_open, note)`。`prev_open` 是后续做 T+1 与信号后移对照的基础。
6. **标的台账入库**：用 `quant-data_get_instruments(refresh=True, include_delisted=True)` 刷新 `instruments`。**必须保留 `delist_date` 非空的退市标的**，否则后续 universe 会犯幸存者偏差。
7. **基本面入库**：用 `quant-data_get_fundamentals(symbols, as_of_date, report_periods)` 补拉；该工具**只返回 `pub_date <= as_of_date` 的记录**，无公告日（`pub_date` 为空）的记录会被丢弃并计入 warnings。这是防前视偏差的锚点，回测取数**必须带 `as_of_date`**。
8. **辅助状态（按需）**：`quant-data_get_limit_status(symbols, trade_date)` 取涨跌停/停牌/一字板；`quant-data_get_index_members(index_code)` 取指数成分股。**注意**：免费源多数只提供当前成分股，工具会如实返回 `point_in_time=false`，历史成分股无法保证，使用时必须显式声明该局限。
9. **回报口径与覆盖度**：汇报每个标的的实际入库区间、行数、复权口径、数据源（`akshare`/`baostock`）、以及 warnings。**本技能到此为止，不做任何判断。**

## 输出格式

- 使用 `templates/dataset_template.md` 的结构：数据需求 → 归一化标的 → 覆盖度核对表 → 入库动作与结果 → 口径与局限声明 → 未完成事项。
- 覆盖度用表格给出：标的 | asset_type | 请求区间 | 已有区间 | 本次补拉 | 入库行数 | 口径 | 状态。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `数据入库_{标的或主题}_{yyyyMMdd}.md`。

## 数据口径（写死，不得违背）

- 代码格式统一 `code.exchange`（`600519.SH` / `510300.SH` / `159915.SZ`）。
- `get_daily_bars` 的 `adjust` 取 `none` / `qfq` / `hfq`。**量（`volume`/`amount`）永远是未复权原始口径**，禁止把"前复权价 × 不复权量"混用——这是最常见也最致命的错误。
- 基本面**必须带 `as_of_date`**，工具只返回 `pub_date <= as_of_date` 的记录。
- 交易单位：A股 / ETF 100 股（份）；科创板 `688` 开头 200 股（`instruments.lot_size` 为准）。

## 上游失败处理（不许重试到挂死）

- 所有上游调用返回统一错误信封 `{"ok": false, "error": {"code", "message", "hint"}}`；常见 `code`：`UPSTREAM_TIMEOUT` / `UPSTREAM_FAILED` / `EMPTY_DB` / `NOT_FOUND` / `BAD_ARGUMENT` / `PUB_DATE_UNAVAILABLE` / `NO_ADJ_FACTOR`。
- 工具内部已带超时（默认 25s）与有限重试（默认 2 次）；**技能层不要再循环重试**。收到 `ok=false` 就把它当终态：记录失败标的、跳过并继续其余标的，最后在 warnings 里汇总。
- 部分标的取不到不影响整单：`get_daily_bars` 在取空的 symbol 下给 `error` 字段，其余照常返回，照常入库。

## 禁止事项

- 禁止编造行情、财务数值或填充"占位数据"；取不到就如实报缺。
- 禁止在 `adjust` 口径不明时混用价格；禁止复权价与原始量交叉使用。
- 禁止丢弃 `delist_date` 非空的标的（幸存者偏差源头）。
- 禁止省略 `as_of_date` 后把基本面当"最新值"用于历史；禁止用 `pub_date` 晚于观察日的财报。
- 禁止把 `get_index_members` 返回的当前成分股当成历史成分股（必须声明 `point_in_time=false`）。
- 禁止对失败的上游调用反复重试、循环等待或伪造成功；本技能不做分析判断、不产出信号、不下单。
