# quant-agent · A股 / ETF 量化交易智能体

面向**个人量化研究者与半自动交易的散户**的量化交易智能体：把「我想验证一个策略」收敛成
**口径清楚、可复现、经独立风控复核**的一条链路 —— 取数入库 → 行业研判 → 因子筛选 → 回测 →
信号复核 → 风控限额 → 受限下单。要动真金白银之前，先过独立风控复核与本地下单闸门。

> 这是一个跑在 [DeerFlow](https://github.com/bytedance/deer-flow)（字节跳动开源、基于 LangGraph 的 Agent 框架）**之上的行业 Agent 资产包**。
> 本仓库**不包含、也不是** DeerFlow 框架本体，而是一套可一键挂载到已有 DeerFlow 实例上的「人设 + 技能 + 领域库 + 子智能体」原创配置与内容。运行时、模型调用、工具与 Web UI 均由 DeerFlow 提供。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 🗄️ 数据运维 | 查本地覆盖度 → 只补缺口 → 落 `quant-db`；代码统一 `code.exchange`、复权口径显式声明、退市标的不丢、基本面必带公告日 |
| 🔍 市场研究 | 行业/标的/ETF 的基本面与量价研判，结论先行、证据可追溯、带反方证据与不确定性；不产出下单指令 |
| 🧮 因子筛选 | 给定 universe 与因子做横截面打分排名、产出候选池，落 `factor_values`（必要时落 `signals`）；不评历史收益 |
| 📈 策略回测 | 含成本模型、数据快照日与复现哈希，强制做「信号后移 1 日」对照与样本外验证；结论必须经 `risk-reviewer` 复核 |
| 📋 信号复核 | 读 `signals`、核可成交性（停牌/涨跌停/一字板/T+1/最小单位）、对照限额编排调仓执行清单；**不含下单** |
| 🛡️ 风控限额 | 维护 `strategies.risk_limits_json`，给单标的/行业集中度与流动性约束；**不给放宽风控或加杠杆的捷径** |
| 🔐 受限下单 | 唯一持有 `trade-gateway_*` 工具的技能；强制「看模式 → 验凭证 → 拿确认码 → 用户贴回 → 下单」五步，四道代码闸门 |

## 旗舰特性一：风控复核子智能体

主助手（lead agent）与一个**只读、不可再委托**的 `risk-reviewer` 子智能体协作。它不是装饰性的
「二次确认」，而是下单凭证的唯一来源：

```
用户提问 / 回测交付 / 调仓计划
        │
        ▼
quant-agent（主助手：7 技能 + 3 MCP，temperature=0.2）
        │  回测跑完要交付，或执行清单要进入下单
        │  通过 task 工具委托（白名单：仅 risk-reviewer）
        ▼
risk-reviewer（资深量化风控，只读复算）
        │  工具：read_file + quant-db_list_tables / describe_table / read_query
        │  disallowed_tools: task / write_file / quant-db_write_query
        │  11 个维度逐条核对；只审风控，不改写策略逻辑
        ▼
三档结论：通过 / 补充措施后可执行 / 不建议执行
        │
        ▼
主助手用 quant-db_write_query 落 risk_reviews
（review_id + verdict + findings_json + lag_shift_test）
        │
        ▼
trade-gateway_prepare_order 去查这张表
        │  可执行档 → 生成 6 位确认码
        │  否则     → 直接拒绝，不生成确认码
```

复核覆盖的 11 个维度：前视偏差、幸存者偏差、过拟合、样本内外、绩效、集中度、流动性、
可成交性、交易成本、杠杆与资金、红线。

**复核结论是硬凭证而非装饰。** 它会真的写进 `risk_reviews` 表，而 `trade-gateway_prepare_order`
会真的去查这张表 —— 少了 `risk_review_id`、记录不存在、结论不是可执行档、或凭证指向的标的
与本次下单不一致，都不会生成确认码。子智能体本身没有任何写库工具，所以「自己给自己发一张
通过凭证」这条路在代码层就被堵死了。

## 旗舰特性二：下单四道闸

| # | 闸门 | 落点 | 具体约束 |
|---|---|---|---|
| 1 | **默认关闭** | `extensions_config.json` + 环境变量 | MCP 条目 `enabled:false`；只有 `install.ps1 -EnableTrading` 才写 `true`；`QUANT_TRADE_DRY_RUN` 默认 `1` —— 即便打开也只写本地 `orders` 表、不报券商 |
| 2 | **风控凭证硬闸门** | `gates.check_risk_review` | `prepare_order` 必须带 `risk_review_id`，且记录真实存在、`verdict` 落在可执行档（`通过` / `补充措施后可执行`），`不建议执行` 一律拒绝；给定 `signal_id` 时还要求凭证目标一致（防「拿 A 的复核下 B 的单」） |
| 3 | **6 位人工确认码** | `gates.create_intent` / `consume_intent` | `prepare_order` 返回 `intent_id` + 6 位数字 `confirm_token`，TTL 120 秒、单次消费，完整生命周期落 `order_intents`；确认码必须由用户从对话里贴回，模型不得编造、不得循环重试 |
| 4 | **下单前重校验** | `gates.consume_intent` + `check_limits` | `place_order` 重查 token 时效与消费状态、风控凭证、单笔金额上限、最小交易单位整数倍、涨跌停越界、停牌/退市；`dry_run=1` 时只写 `orders` 且 `xt_order_id` 必须为空 |

**闸门落在 MCP 代码层，而不是提示词层。** 提示词里写「必须先复核」「不许自己下单」是可以被
模型绕过的 —— 换一种说法、多轮诱导、跳过某个技能，都可能让软约束失效。所以真正拦人的是
`prepare_order` / `place_order` 里的 Python 分支与 `GateReject` 异常码，他们不认识「模型觉得
可以」，只认 `risk_reviews` 里的记录和 `order_intents` 里的状态位。

## 领域知识库（quant-db，15 张关联表）

通过 `uvx --with mcp==1.2.1 mcp-server-sqlite` 挂载一个本地 SQLite 库，工具以
`quant-db_` 为前缀暴露。主链路：

```
instruments ─┬─► bars_daily ─┐
             ├─► fundamentals ┼─► factor_values ─► strategies
             └─► trading_calendar                    │
                                                     ├─► backtests ─► equity_curve
                                                     │        │
                                                     │        ▼
                                                     └─► signals
                                                              │
                            risk_reviews ◄──── 复核结论落库 ──┤
                                                              ▼
                                                    order_intents（确认码）
                                                              │
                                                           orders ─► trades
                                                              │
                                                     positions_daily（日终快照）
```

| 表名 | 关键字段 | 设计要点 |
|---|---|---|
| `instruments` | `symbol` PK, `exchange`, `asset_type`, `board`, `industry_sw1`, `list_date`, `delist_date`, `lot_size`, `is_st`, `index_code`, `status` | **保留 `delist_date` 非空的退市标的 —— 治幸存者偏差**；`board` 决定涨跌幅（main ±10%、chinext/star ±20%、bse ±30%、etf ±10%）；`lot_size` 科创板为 **200** |
| `trading_calendar` | `cal_date` PK, `is_open`, `prev_open`, `note` | `prev_open` 是 T+1 成交与「信号后移对照」的基础 |
| `bars_daily` | `symbol`+`trade_date` PK, OHLC, `pre_close`, `volume`, `amount`, `turnover_rate`, `adj_factor`, `adjust_mode`, `source` | 价格带**累计复权因子**；`volume`/`amount` 恒为未复权原始口径；`source` 记录 akshare / baostock / manual |
| `fundamentals` | `symbol`+`report_period` PK, **`pub_date`**, `pe_ttm`, `pb`, `roe`, `revenue_yoy`, `profit_yoy`, `mkt_cap` | **`pub_date` 是防前视的锚点**：as-of 查询只允许看到 `pub_date <= as_of` 的财报，无公告日的记录直接丢弃 |
| `factor_definitions` | `factor_name` PK, `formula`, `direction`, `params_json`, `lookback_days`, `category` | 公式与方向落库，禁止凭记忆编造因子 |
| `factor_values` | `factor_name`+`symbol`+`trade_date` PK, `raw_value`, `z_score`, `pct_rank` | 横截面标准化只在同一 `trade_date` 内进行 |
| `strategies` | `strategy_id` PK, `universe_json`, `params_json`, `risk_limits_json`, `rebalance_rule` | 参数与风控限额同库同源 |
| `backtests` | `run_id` PK, `cost_model_json`, `param_grid_size`, **`data_snapshot_date`**, **`repro_hash`**, **`lag_shift_checked`** | **可复现**：数据快照日 + 代码/参数/快照哈希；未做后移对照降档，成本缺失直接不通过 |
| `equity_curve` | `run_id`+`trade_date` PK, `nav`, `bench_nav`, `drawdown` | 净值与基准同表对齐，回撤逐日留痕 |
| `signals` | `signal_id` PK, `trade_date`, `symbol`, `side`, `score`, `target_weight`, `status`, `risk_review_id` | `trade_date` 是**信号生成日**，成交必须在下一交易日；`status` 走 new → reviewed → approved |
| `risk_reviews` | `review_id` PK, `target_type`, `target_id`, `verdict`, `findings_json`, `lag_shift_test` | **下单硬闸门的唯一凭证来源** |
| `order_intents` | `intent_id` PK, `expires_at`, `consumed_at`, `status`, `confirm_token`, `risk_review_id` | 确认码的完整生命周期：TTL / 单次消费 / 拒绝原因 |
| `orders` | `order_id` PK, `intent_id`, `status`, `is_dry_run`, `xt_order_id`, `fee`, `tax`, `transfer_fee` | `dry_run` 单必须 `status='dry_run'` 且 `xt_order_id` 为空 |
| `trades` | `trade_id` PK, `order_id`, `price`, `qty`, `fee`, `tax`, `trade_time` | 成交明细，`xt_trade_id` 对得上券商回报 |
| `positions_daily` | `account_id`+`trade_date`+`symbol` PK, `qty`, `avail_qty`, `avg_cost`, `market_value`, `weight` | 日终快照；`avail_qty` 体现 T+1 可卖约束 |

单位约定：金额=人民币元；数量=股/份；比率类字段一律小数（`0.12` 表示 12%）；日期 `YYYY-MM-DD`；
时间戳为 Unix 秒。

## 数据口径（务必先读）

口径错了，后面所有因子、回测与复核都是错的 —— 这是量化里最贵的一类 bug。

- **价格复权**：`get_daily_bars` 的 `adjust` 取 `none` / `qfq` / `hfq`，默认 `qfq`。库里
  `adj_factor` 统一存**累计复权因子（后复权口径）**，`qfq = price × adj_factor / latest_factor`。
- **量恒未复权**：`volume` / `amount` 永远是原始未复权口径，**任何情况下都不要对成交量做复权
  换算**；同一组计算里的价格必须声明口径（`none` / `qfq` / `hfq`），不可中途换口径。
- **基本面必带 `as_of_date`**：工具只返回 `pub_date <= as_of_date` 的记录；无处公告日的记录丢弃
  并计入 warnings。回测调仓取数不带 `as_of_date` 即为前视。
- **信号 T+1 成交**：`signals.trade_date` 是信号生成日，成交必须在**下一交易日**；回测不允许用
  当日收盘价成交。
- **最小交易单位**：A股 / ETF 100 股（份）；**科创板 `688` 开头 200 股**（以
  `instruments.lot_size` 为准）。
- **成本模型**：佣金万 2.5（**最低 5 元**）、卖出印花税 **0.05%**、过户费 **0.001%**、滑点；
  `backtests.cost_model_json` 与 `gates.estimate_costs` 必须同口径。
- **指数成分**：免费源多只提供当前成分股，`get_index_members` 会如实返回 `point_in_time=false`，
  历史回测据此判定引入成分股前视。
- **不可判定即不可交易**：`get_limit_status` 取不到前收、或 `tradable=false`，一律按不可成交处理。

## 目录结构

```
quant-agent/
├─ README.md                            # 中文主文档
├─ README_EN.md                         # 英文完整版
├─ LICENSE                              # MIT + 投资风险附加声明
├─ .gitignore                           # 排除 *.db / venv / 本机配置
├─ persona/SOUL.md                      # 人设：角色、口径纪律、复核委托规则、免责
├─ config/agent.config.yaml             # Agent 配置（7 技能 / 3 MCP / 子代理白名单 / temperature=0.2）
├─ skills/                              # 7 个领域技能（SKILL.md + 可填写模板）
│  ├─ data-ops/                         #   取数入库（templates/dataset_template.md）
│  ├─ market-research/                  #   行业与标的研判（templates/research_template.md）
│  ├─ factor-screening/                 #   因子打分与候选池（templates/screening_template.md）
│  ├─ strategy-backtest/                #   回测与绩效评估（templates/backtest_template.md）
│  ├─ signal-review/                    #   信号复核与执行清单（templates/execution_template.md）
│  ├─ live-trading/                     #   受限下单（templates/order_template.md）
│  └─ risk-limits/                      #   风控参数与限额（templates/risk_limits_template.md）
├─ mcp-servers/                         # 2 个自建 MCP server（Python / stdio）
│  ├─ quant-data/                       #   server.py + normalize.py（口径与错误信封）
│  └─ trade-gateway/                    #   server.py + gates.py（四道闸的纯函数实现）
├─ integrations/
│  ├─ quant-data/mcp.example.json       # 取数 MCP 配置片段（含 UTF-8 关键设置）
│  ├─ quant-db/
│  │  ├─ schema.sql                     # 15 张表 DDL + 常用检索 SQL
│  │  ├─ seed.sql                       # 演示种子（不含任何行情/财务数值）
│  │  └─ mcp.example.json               # 状态库 MCP 配置片段
│  └─ trade-gateway/mcp.example.json    # 交易 MCP 片段（enabled:false + PYTHONPATH 注入 xtquant）
├─ subagents/risk-reviewer.yaml         # 风控复核子智能体（4 空格缩进片段 + 幂等标记块）
├─ scripts/
│  ├─ install.ps1                       # Windows 一键安装器（9 步，幂等）
│  └─ check_consistency.py              # 一致性校验（离线，8 类检查）
├─ tests/                               # pytest（离线，不联网、不需要 QMT）
│  ├─ conftest.py                       #   用真实 schema.sql 建库，按文件路径加载被测模块
│  ├─ test_order_gate.py                #   四道闸的拒绝路径
│  ├─ test_data_ops.py                  #   代码/板块/涨跌停/复权/错误信封
│  ├─ test_risk_reviewer_fixtures.py    #   复核提示词覆盖度 + 3 错 1 对判定回归
│  └─ fixtures/risk_reviewer/           #   clean / future_function / missing_cost_model / no_out_of_sample
├─ examples/                            # 3 个可复现操作样例（runbook，非伪造对话）
│  ├─ 01-data-ops-runbook.md
│  ├─ 02-backtest-with-risk-review.md
│  └─ 03-dry-run-order-two-step.md
└─ docs/architecture.md                 # 架构、闸门设计、挂载点与踩坑记录
```

## 环境要求

- 一个可运行的 [DeerFlow](https://github.com/bytedance/deer-flow) 实例（本仓库在 DeerFlow 主分支
  后端 + Web 网关上验证）
- Python 3.12、[uv/uvx](https://docs.astral.sh/uv/)（用于拉起 MCP server 与建独立 venv）
- 一个 LLM API Key（DeerFlow 支持的模型均可；**不需要**联网搜索专用 Key，默认走免 Key 的
  DuckDuckGo）
- 实盘可选：QMT / MiniQMT 客户端（xtquant 随客户端分发，**不在 PyPI**），以及券商开通的量化
  交易权限
- Windows / macOS / Linux 均可；安装脚本与排障说明主要面向 **Windows + 中文系统**

## 安装

### Windows 一键（推荐）

以普通 PowerShell 运行（脚本默认对接 `d:\...\deepflow\deer-flow`，可用 `-DeerFlowRoot` 覆盖）：

```powershell
.\scripts\install.ps1
# 或指定实例与用户：
.\scripts\install.ps1 -DeerFlowRoot "D:\path\to\deer-flow" -UserId "<your-user-id>"
# 启用实盘网关（仍然默认 dry_run，不报券商）：
.\scripts\install.ps1 -EnableTrading -QmtPath "C:\国金QMT" -QmtAccount "你的资金账号"
# 卸载（保留 quant.db 与 venv）：
.\scripts\install.ps1 -Uninstall
```

安装器幂等地完成 **9 步**：

1. 复制 Agent（`SOUL.md` + `config.yaml`）与 7 个技能到用户空间 `agents/quant-agent/` 与 `skills/custom/`；
2. 把 `mcp-servers/*` 复制到 `<deer-flow>\backend\.deer-flow\mcp\quant-agent\`（此后仓库可移动，安装不再依赖仓库路径）；
3. 为 `quant-data` 建独立 venv 并安装 `akshare` / `baostock` / `mcp>=1.28,<2`（与 machine-db 锁定的 `mcp==1.2.1` 环境隔离；此步联网且最慢，失败不阻塞后续）；
4. 合并 `quant-data` / `quant-db` / `trade-gateway` 三个 MCP 与 7 个 skill 开关到 `extensions_config.json`（写入本机绝对路径，`trade-gateway` 默认 `enabled:false`）；
5. 创建 `quant.db`（15 张表），**仅在 `instruments` 为空时**灌入演示种子；
6. 以 `# BEGIN/END quant-agent managed block` 幂等注入 `risk-reviewer`，**插到既有 `custom_agents:` 之下**，不新增第二个顶层 `subagents:` 键；
7. 环境自检（`uv.exe` / `uvx.exe` / QMT 路径 / 8001 端口占用）；
8. 把 `web_search` 由 tavily（需 Key）切换为 DuckDuckGo（`-UseDuckDuckGo:$false` 可保留原后端）；
9. 收尾提示：重启 Gateway、先跑一次 data-ops 取数、校验子代理键、卸载方式。

可用参数：`-DeerFlowRoot`、`-UserId`、`-EnableTrading`、`-QmtPath`、`-QmtAccount`、`-UseDuckDuckGo`、
`-SkipSubagent`（仅调试用）、`-Uninstall`。

随后**重启 Gateway**（配置不热加载），在新会话中选择「**量化交易助手**」。安装前会备份被改动的
`config.yaml` 与 `extensions_config.json`（同目录 `*.bak_<时间戳>`）。

### 手工安装

1. 把 `persona/SOUL.md` 与 `config/agent.config.yaml` 放到用户空间 `agents/quant-agent/`
   （配置文件重命名为 `config.yaml`）；
2. 把 `skills/*` 整目录复制到用户空间 `skills/custom/`；
3. 把 `mcp-servers/` 复制到 `<deer-flow>\backend\.deer-flow\mcp\quant-agent\`；
4. 按三个 `integrations/*/mcp.example.json` 注释，把 `mcpServers` 与 `skills` 两段合并进
   `deer-flow/extensions_config.json`，并把 `command`、`QUANT_DB_PATH`、`PYTHONPATH` 换成你本机的
   绝对路径；
5. `sqlite3 <db> < integrations/quant-db/schema.sql`，空库时再灌 `seed.sql`；
6. 把 `subagents/risk-reviewer.yaml` 两个标记之间的 **4 空格缩进片段**插到 `config.yaml` 既有
   `subagents.custom_agents:` 之下。

细节与挂载点见 [docs/architecture.md](docs/architecture.md)。

## 自动化校验与测试

两条命令，都**离线可跑**，不需要联网、不需要 QMT、不需要模型：

```powershell
python scripts/check_consistency.py    # 跨文件一致性（8 类检查）
python -m pytest tests -q              # 单元与回归测试
```

`check_consistency.py` 校验的 8 类：① 配置里引用的技能 / MCP / 子代理都有对应文件；
② 每个 `SKILL.md` 的 `allowed-tools` 逐项在已声明工具清单内；③ **除 `live-trading` 外任何技能
都不得含 `trade-gateway_` 前缀工具**；④ `schema.sql` 建出的表覆盖技能与 MCP 里 SQL 引用的表名；
⑤ `install.ps1` 引用的仓库路径真实存在、开关与默认值正确；⑥ `trade-gateway` 在示例与安装器里
都默认关闭；⑦ `risk-reviewer.yaml` 是缩进片段、工具在清单内、禁写禁再委托；⑧ `schema.sql` 恰好
15 张表、交付物齐全。

`tests/` 的 **88 个用例**覆盖三类：

- **下单闸门**（`test_order_gate.py`）：确认码 TTL 过期、重复消费、缺 `risk_review_id`、
  `verdict` 非可执行档、`dry_run` 不报单这五种拒绝路径，外加猜错确认码不消耗、过期 pending
  意图清理、单笔限额 / 整手 / 停牌 / 涨跌停越界、仓位上限与费用模型口径。
- **口径归一化**（`test_data_ops.py`）：代码归一化（含 `000001` 沪深同号）、板块推断、涨跌幅
  限制、涨跌停价四舍五入、复权换算、上游中英文行归一化、结构化错误信封与拒绝码稳定性。
- **风控复核**（`test_risk_reviewer_fixtures.py`）：断言提示词写全 11 个维度与三档结论、必须
  含「信号后移 1 个交易日」与「`cost_model_json` 缺失直接不通过」，并用 4 个 fixture
  （**3 错 1 对**）验证硬判据不会把已知错误样例判过。

## 实测情况（本地端到端）

只报告可复现的工程结果：

- `python -m pytest tests -q` → **88 passed**；
- `python scripts/check_consistency.py` → 全部检查通过；
- 两个自建 MCP server 可被 MCP 客户端正常拉起，含中文的结果（如「沪深300ETF」「通过」）在
  `PYTHONUTF8=1` 下正常返回，不出现 60 秒超时；
- `trade-gateway` 在 `dry_run=1` 下可以完整走通 `prepare_order` → 用户贴回确认码 → `place_order`，
  `orders` 表出现 `status='dry_run'` 且 `xt_order_id` 为空、券商无报单。

> 这里**不提供**任何回测收益、夏普、回撤或行情数字 —— 那些必须由你用自己的数据跑出来。
> 不同模型在工具调用与委托触发上可能有差异，生产使用前请按自己的数据与制度自行验收。

[`examples/`](examples/) 下是 3 个**可复现的操作样例（runbook）**，不是伪造的对话记录或回测结果：
`01` 演示建标的池并补齐日线并核对覆盖度与口径；`02` 演示一次 ETF 动量轮动回测如何落库并送
`risk-reviewer` 复核；`03` 演示 `dry_run` 下单的两步走与各类拒绝码。

## 安全与免责（务必阅读）

- 本项目是**量化研究与自动化工具，不构成任何投资建议**。`SOUL.md` 明确禁止给出「稳赚」「必涨」
  之类的确定性承诺，也禁止编造行情、收益率、因子 IC 或回测结果。
- **历史回测不代表未来收益。** 实盘盈亏与由此产生的任何后果由使用者自行承担。
- 默认**不启用交易网关**（`enabled:false`），且启用后仍默认 `QUANT_TRADE_DRY_RUN=1`，只写本地
  `orders` 表不报券商。要真报单必须显式改 `QUANT_TRADE_DRY_RUN=0`，并自行确认已按券商与监管
  要求开通量化交易权限、签署相关协议。
- 演示种子（`seed.sql`）只含公开的代码 / 名称 / 板块 / 行业等事实，**不含任何行情价格与财务
  数值**；`backtests` / `signals` / `orders` 等表刻意留空，必须由真实流程写入。
- 本项目以 MIT 许可发布；DeerFlow 框架遵照其各自许可。

## Windows 中文系统排障

- **现象**：MCP 工具在纯 ASCII 结果上正常，一旦返回含中文的内容就 60 秒超时。
  **原因**：stdio 子进程在中文 Windows 默认按 GBK(cp936) 写 stdout，MCP 客户端按 UTF-8 解码失败。
  **解决**：三个 MCP 的 `env` 都要写 `PYTHONUTF8=1`（最好再加 `PYTHONIOENCODING=utf-8`），
  **不是只给 sqlite 加**；安装器已自动注入，手工安装别漏。
- **现象**：技能加载后，MCP 或 `task` 工具报 “not allowed by the active skill policy”。
  **原因**：技能 `allowed-tools` 是精确白名单，**YAML 列表形式不做别名归一化**。写
  `Read/Write/WebSearch` 不会匹配任何运行时工具。
  **解决**：一律写运行时真名，如 `read_file`、`write_file`、`web_search`、`task`、
  `quant-data_get_daily_bars`、`quant-db_read_query`、`trade-gateway_prepare_order`。
- **现象**：`quant-db` MCP 起不来或工具列表为空。
  **原因**：新版 MCP SDK 移除了旧 sqlite server 依赖的 `Server.list_resources` 装饰器。
  **解决**：锁定 `uvx --with mcp==1.2.1 mcp-server-sqlite`；`quant-data` 用独立 venv 装
  `mcp>=1.28,<2`，两者互不影响。
- **现象**：装完 `risk-reviewer` 后 `config.yaml` 解析失败，或原有 `safety-reviewer` 消失。
  **原因**：本机 `config.yaml` 已有 machine-problems 注入的顶层 `subagents:` 键，再追加第二个
  同名顶层键会重复 / 静默覆盖。
  **解决**：`subagents/risk-reviewer.yaml` 只是 **4 空格缩进的片段**，必须插到既有
  `custom_agents:` 行之后（安装器已这么做）；校验方法：

  ```powershell
  uv run --with pyyaml python -c "import yaml;d=yaml.safe_load(open(r'<deer-flow>\config.yaml',encoding='utf-8'));print(list(d['subagents']['custom_agents'].keys()))"
  # 应同时看到 safety-reviewer 与 risk-reviewer
  ```

  > 注意：`risk-reviewer` 是自定义 `custom_agents` 条目，`allowed_subagents` 只授权这一个 ——
  > 主助手看不到也不能调用其它子智能体。
- **现象**：`quant-data` 首次调用任一取数工具（交易日历、日线、基本面）卡 60~100 秒后超时，
  而同一台机器上直接 `python -c "import akshare"` 只要 2 秒左右。
  **原因**：在 MCP 的工作线程里首次导入 akshare 时，其依赖 `numpy._core._multiarray_umath`
  在该上下文下初始化异常缓慢（实测 76~100 秒）；在 Python 3.14 的 venv 上尤其明显。
  **解决**：一是 venv 锁 Python 3.12（安装器已用 `uv venv --python 3.12`）；二是
  `server.py` 在 `mcp.run()` 前于主线程预热 `akshare` / `baostock`，把导入成本挪到
  会话初始化阶段（`session_init_timeout` 为 180 秒，足够）。
- **xtquant 不在 PyPI**：它随 QMT 客户端分发，同名包多为非官方上传。安装器不执行任何
  `pip install`，而是把 `<QmtPath>\bin.x64\Lib\site-packages` 通过 `PYTHONPATH` 注入。

## License

[MIT](LICENSE) © 2026 chen1255
