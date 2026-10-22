---
name: market-research
description: A股/ETF 行业、标的与主题的研究分析（研究口径）。当用户说"分析一下这个行业/这只股票/这个 ETF""研究一下""这个赛道怎么样""值不值得买（研究口径）""帮我做个基本面/景气度梳理"时使用。产出口径清晰、证据可追溯的研究结论，不生成可执行下单指令。要选股筛选打分/ETF 轮动改用 factor-screening；要回测某个策略的历史收益/年化/回撤改用 strategy-backtest；要执行调仓计划改用 signal-review。
license: MIT
allowed-tools:
  - read_file
  - write_file
  - web_search
  - web_fetch
  - quant-data_get_instruments
  - quant-data_get_daily_bars
  - quant-data_get_fundamentals
  - quant-data_get_index_members
  - quant-db_list_tables
  - quant-db_describe_table
  - quant-db_read_query
  - task
argument-hint: "研究标的或行业（代码/名称/主题）+ 研究目的与时间口径（as_of 日期）"
---

# 市场研究技能（market-research）

把"这个值不值得买"这类开放式问题，收敛成**有边界、有证据、有免责**的研究结论。
本技能给研究判断，不产出信号、不下单、不改动风控参数。

## 触发逻辑

- **触发方式**：对某个标的、行业或主题做基本面/景气度/估值/资金面的研究分析。
- **触发示例**：
  - "帮我分析一下白酒行业现在什么情况"
  - "研究一下 510300.SH 这只 ETF，长期持有怎么样"
  - "新能源车这个赛道值不值得配"
  - "601398.SH 和 000001.SZ 哪个基本面更强（研究口径）"
- **不触发**：要按因子筛选/打分排名/ETF 轮动 → `factor-screening`；要评估某个策略的历史收益（年化/夏普/回撤） → `strategy-backtest`；要形成调仓执行清单 → `signal-review`。

## 输入要求（信息不足先问，不要猜）

一次最多问 **2–3 个**：

| 信息 | 为什么关键 |
|---|---|
| 研究标的/行业/主题 | 决定是单标的深挖还是行业比较；主题需先映射到标的或指数 |
| 研究目的 | 长期配置 / 短期景气 / 估值对比，决定看哪些维度 |
| 时间口径（as_of 日期） | 研究以哪一天的信息为准；基本面必须按 `pub_date <= as_of` 取 |
| 比较基准或同类 | 有对比才有结论；无则默认用宽基指数（如 000300.SH）作参照 |

## 工作流程（严格按序，不跳步）

1. **明确研究边界**：确认研究对象、目的、as_of 日期与基准。把"主题"先落成可查的标的或指数（用 `quant-data_get_instruments` / `quant-data_get_index_members`）。
2. **拉标的与成分**：`quant-data_get_instruments`（`include_delisted=True`）拿到标的属性（板块、行业 `industry_sw1`、`lot_size`）；行业研究用 `quant-data_get_index_members` 取成分股，并**记住 `point_in_time=false`**——免费源只有当前成分。
3. **取行情做趋势与波动**：`quant-data_get_daily_bars(symbols, start, end, adjust="qfq")` 看价格趋势、区间涨跌、成交量能变化。价格用复权口径，**量永远是未复权原始口径**，不可交叉。
4. **取基本面并绑定 as_of**：`quant-data_get_fundamentals(symbols, as_of_date=as_of)` 拿估值（`pe_ttm`/`pb`）、质量（`roe`）与成长（`revenue_yoy`/`profit_yoy`）。**必须带 `as_of_date`**，只允许用观察日前已公告的财报，避免前视。
5. **查本地补充信息**：用 `quant-db_read_query` 读 `instruments` / `bars_daily` / `fundamentals` 做交叉核对（例如库里的行业分类、覆盖度）。
6. **外部信息补充**：需要政策、产业、事件等信息时用 `web_search` / `web_fetch`，**逐条标注来源与日期**，与 as_of 不一致的信息要注明。
7. **形成研究结论**：按"结论先行 → 支撑维度（估值/质量/成长/景气/资金/风险）→ 反方证据 → 不确定性"组织。结论用定性 + 区间口径表述。
8. **加免责与边界**：研究结论不是投资建议；**本技能不产出可执行下单指令**，需要执行时移交 `signal-review` / `live-trading`。

## 输出格式

- 使用 `templates/research_template.md` 的结构：研究问题与口径 → 标的与基准 → 支撑维度表 → 反方证据 → 结论与不确定性 → 数据来源与免责。
- 关键判断用表格给出"维度 | 观察 | 证据（引用具体记录/来源） | 方向"，**不得编造数值**，缺失就写"数据不足，不下结论"。
- 用户要求存档时用 `write_file` 写到 `/mnt/user-data/outputs/`，文件名 `研究_{标的或主题}_{as_of}.md`。

## 数据口径（写死，不得违背）

- 代码统一 `code.exchange`（`600519.SH` / `510300.SH` / `159915.SZ`）。
- 基本面**必须带 `as_of_date`**，只用 `pub_date <= as_of_date` 的财报；查不到公告日的记录一律不用。
- 复权只用于价格比较；`volume`/`amount` 恒为未复权原始口径，禁止"前复权价 × 不复权量"混用。
- 使用指数成分时声明 `point_in_time=false`：免费源只提供当前成分，历史研究存在成分前视。

## 禁止事项

- 禁止编造行情数字、财务指标、收益率、评级或目标价；没有数据就写"数据不足"。
- 禁止绕开 `as_of_date` 用"最新财报"回溯历史结论（前视偏差）。
- 禁止把研究结论直接表述成买入/卖出指令；禁止涉及具体下单数量、价格与下单动作。
- 禁止用当前成分股冒充历史成分股。
- 禁止给出"稳赚""必涨"等确定性承诺；只给有证据、有边界、带不确定性的研究判断。
- 本技能为研究口径；**用户明确要求下单或执行调仓时，移交 `signal-review`（执行计划）或 `live-trading`（下单）**，本技能不做执行。
