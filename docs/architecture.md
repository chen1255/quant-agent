# 架构说明

本文讲清楚五件事：这个仓库**是什么**、它如何**挂载**到 DeerFlow 运行时、三个 MCP 与 15 张表如何协作、
**为什么下单闸门必须落在 MCP 层**，以及开发中实测到的坑与已知边界。

## 1. 定位：资产包，不是框架的 fork

DeerFlow 提供运行时（LangGraph agent 工厂、模型接入、工具系统、技能加载、MCP 客户端、子智能体、
Web/网关）。本仓库只提供运行在其上的**行业内容与配置**，不含框架代码：

| 关注点 | 归属 |
|---|---|
| Agent 运行循环、模型调用、工具执行、MCP/stdio、Web UI、鉴权 | DeerFlow 框架 |
| 量化人设、7 个领域技能、15 表库结构与种子、风控复核子智能体、安装器与离线测试 | **本仓库（原创）** |

好处：框架可独立升级；本仓库通过 DeerFlow 既有扩展点装配，不改框架源码。

## 2. 整体架构

```
User
  │
  ▼
DeerFlow Runtime（LangGraph agent 工厂 / 模型接入 / 技能加载 / MCP 客户端 / 子智能体 / Web 网关）
  │
  ▼
quant-agent（lead agent）
  │  name: quant-agent   display_name: 量化交易助手
  │  temperature=0.2  thinking_enabled=true  memory_enabled=false
  │
  ├─ 技能层（7 个，按需加载，互指不重叠）
  │   data-ops · market-research · factor-screening · strategy-backtest
  │   signal-review · live-trading · risk-limits
  │
  ├─ MCP 层（3 个 server，工具池按职责切分）
  │   quant-data     取数（只读语义 + 本地缓存）      enabled=true
  │   quant-db       状态库读写（uvx sqlite）          enabled=true
  │   trade-gateway  账户/持仓/下单/撤单（四道闸）     enabled=false
  │
  └─ 子智能体层（1 个，白名单）
      risk-reviewer  只读复算、不可再委托、不可写库
```

主 Agent 的 `allowed_subagents: [risk-reviewer]` 是**白名单**：既看不到内置通用子智能体，也不能
自行扩权。`risk-reviewer` 的工具只有 `read_file` + `quant-db_list_tables` / `describe_table` /
`read_query`，且 `disallowed_tools: ["task", "write_file", "quant-db_write_query"]` —— 它只能在
给定方案上做只读复算，无法联网、无法写库、无法再分派。

### Agent 配置的三个取舍

- `temperature: 0.2`：量化结论要确定性、低发散，不接受「差不多」的数字。
- `thinking_enabled: true`：回测与复核需要多步推理（口径、对照、成本）。
- `memory_enabled: false`：关闭跨会话长期记忆，避免把「某次行情的巧合」沉淀成跨策略的错误经验；
  一切事实以 `quant-db` 为准。

## 3. 三个 MCP：职责与权限分离

| Server | 类型 | 职责 | 默认 | 工具数 |
|---|---|---|---|---|
| `quant-data` | 自建 stdio（`mcp-servers/quant-data/server.py`） | 交易日历、标的台账、日线、基本面、涨跌停/停牌、指数成分；唯一写入是缓存落 `bars_daily` / `fundamentals` / `instruments` | **开** | 6 |
| `quant-db` | `uvx --with mcp==1.2.1 mcp-server-sqlite` | 15 张表的通用 SQL 读写，承载因子、回测、信号、复核、意图、订单、成交、持仓 | **开** | 6 |
| `trade-gateway` | 自建 stdio（`mcp-servers/trade-gateway/server.py`） | 交易模式、账户、持仓、委托、成交查询，以及 `prepare_order` / `place_order` / `cancel_order` | **关** | 8 |

`quant-data` 的 6 个工具：`get_trading_calendar`、`get_instruments`、`get_daily_bars`、
`get_fundamentals`、`get_limit_status`、`get_index_members`。全部是**只读语义**，副作用仅限于把
上游结果落本地缓存。

`trade-gateway` 的 8 个工具：`get_trading_mode`、`get_account`、`get_positions`、`get_orders`、
`get_trades`、`prepare_order`、`place_order`、`cancel_order`。

### 为什么交易必须是独立的第三个 server

1. **工具池隔离**：下单工具不能与研究 / 回测会话共用同一个工具池。三 server 切分后，权限边界
   就是 server 边界，`allowed-tools` 只需在技能层再收一道。
2. **依赖隔离**：`xtquant` 依赖本机 QMT 客户端，只在安装了 QMT 的机器上可导入。若把它混进取数
   server，任何没装 QMT 的机器上**整个数据面都起不来**。现在 `trade-gateway` 连不上时返回结构化
   `unavailable`，`quant-data` 照常工作。
3. **默认关的开关**：per-server `enabled` 是唯一干净、可审计、可一键撤销的「默认关闭」开关。
   把它写成提示词里的「不要下单」，等于没有开关。
4. **失败面隔离**：交易侧崩了不会拖垮取数与状态库；取数侧上游抖动也不会影响已落的订单与凭证。

`quant-data` 和 `quant-db` 用**两套 Python 环境**：前者是独立 venv（`akshare` / `baostock` /
`mcp>=1.28,<2`），后者是 `uvx --with mcp==1.2.1`。这样 machine-db 锁定的 `mcp==1.2.1` 与本包
所需的 MCP SDK 版本互不影响。

## 4. 数据流

```
上游 akshare / baostock
   │  quant-data_get_*（超时 + 有限重试；失败返回结构化错误信封）
   ▼
quant-db
   ├─ instruments / bars_daily / fundamentals / trading_calendar   ← 取数缓存
   ├─ factor_values（横截面 z_score / pct_rank）
   ├─ strategies（universe / params / risk_limits）
   ├─ backtests + equity_curve（含 data_snapshot_date / repro_hash / lag_shift_checked）
   ├─ signals（trade_date = 信号生成日；成交必须在下一交易日）
   │        │
   │        └── task(risk-reviewer) ──► verdict / findings_json
   │                                        │
   │                                        ▼
   │                                   risk_reviews（唯一凭证）
   │                                        │
   ├─ order_intents（6 位 confirm_token / expires_at / consumed_at）
   │        │
   │        ▼
   ├─ orders（dry_run 时 status='dry_run'、xt_order_id 为空）
   │        │
   │        ▼
   ├─ trades
   └─ positions_daily（日终快照，avail_qty 体现 T+1）
```

关键约束：行情只有一条入口（`quant-data`），状态只有一处落点（`quant-db`），下单只有一条通路
（`trade-gateway` 的两步式）。任何一环想抄近路，都会在表结构或闸门上撞墙。

## 5. 15 张表的关系与设计要点

```
instruments ─1──n─ bars_daily
     │ 1
     ├──n─ fundamentals
     │ 1
     └──n─ factor_values ─n──1─ factor_definitions

strategies ─1──n─ backtests ─1──n─ equity_curve
     │                 │
     │                 └◄── risk_reviews（target_type='backtest'）
     └──1──n─ signals ─◄── risk_reviews（target_type='signal'，signals.risk_review_id）
                  │
                  └──1──n─ order_intents ─1──n─ orders ─1──n─ trades

trading_calendar（独立：T+1 判定、prev_open、信号后移对照）
positions_daily（独立：account_id + trade_date + symbol 日终快照）
```

schema 里显式声明 `FOREIGN KEY` 的有 7 处：`factor_values → factor_definitions`、
`backtests → strategies`、`equity_curve → backtests`、`signals → risk_reviews`、
`order_intents → risk_reviews`、`orders → order_intents`、`trades → orders`。
`instruments` / `bars_daily` / `fundamentals` 之间是 `symbol` 层面的逻辑关联，刻意不加外键 ——
否则「先落行情缓存、后补标的台账」的顺序会被约束死。

四个关键设计点：

1. **`instruments.delist_date` 治幸存者偏差**：退市标的必须留在库里（`status='delisted'`），
   universe 才能包含「当时存在、后来退市」的样本。种子库里就刻意留了一只已退市标的做自检。
2. **`fundamentals.pub_date` 防前视**：`report_period` 是报告期，`pub_date` 是公告日。as-of 查询
   按 `pub_date <= as_of_date` 过滤，只有 `report_period` 没有公告日的记录一律丢弃。
3. **`backtests.data_snapshot_date` + `repro_hash` 保可复现**：前者固定「用的哪天的数据」，
   后者是代码 + 参数 + 数据快照的哈希。两项缺一，这份回测就不具备被复核的资格。
4. **`risk_reviews` 是下单凭证**：`verdict` 只有三档，`target_type` / `target_id` 指向被审对象；
   `order_intents.risk_review_id` 是 `NOT NULL`，把「凭证」焊在下单意图上。

单位约定（写进 schema 注释）：金额=人民币元；数量=股/份；比率一律小数；日期 `YYYY-MM-DD`；
时间戳 Unix 秒。所有 `*_json` 字段统一存 JSON 字符串，便于 `check_consistency.py` 静态校验。

## 6. 为什么闸门必须落在 MCP 层

这是整个项目最重要的一个设计判断，对应文件是 `mcp-servers/trade-gateway/gates.py`。

**提示词层的约束是可以被绕过的。** 在 `SOUL.md` 或 `SKILL.md` 里写「下单前必须先复核」，本质上
是一段自然语言建议：换一种问法、装可怜、声称「已经复核过了」、或者干脆让模型跳过 `live-trading`
技能直接调工具，软约束都可能失效。更现实的是，模型并不知道自己「没有权限」——它只知道有个工具
可以调。

**代码层的约束不会被绕过，因为它根本不听模型解释。** 四道闸的落点：

| 闸 | 代码位置 | 拒绝时发生什么 |
|---|---|---|
| 1 默认关闭 | `extensions_config.json` 的 `enabled:false`；`QUANT_TRADE_DRY_RUN` 默认 `1` | 工具根本不在工具池里；或即便在，也只写本地表 |
| 2 风控凭证 | `gates.check_risk_review()` | 抛 `GateReject`，`prepare_order` 返回 `blocked_by_gate: true`，**不生成确认码** |
| 3 人工确认码 | `gates.create_intent()` / `consume_intent()` | 6 位数字、TTL 120 秒、单次消费；过期 `INTENT_EXPIRED`、重复 `INTENT_ALREADY_CONSUMED`、猜错 `TOKEN_MISMATCH` |
| 4 下单前重校验 | `consume_intent()` + `gates.check_limits()` | 重查 token、凭证、金额上限、整手、涨跌停、停牌；`dry_run` 下禁止出现券商委托号（`DRY_RUN_LEAK`） |

两个细节值得单独说明：

- **确认码只能由人给。** `place_order` 需要的 `confirm_token` 是 `prepare_order` 随机生成的 6 位
  数字，服务端用 `secrets.compare_digest` 比对。模型没有别的途径知道它 —— 除了把它念给用户、
  再由用户贴回来。这条把「模型自主下单」变成了「模型必须请求人类批准」。
- **凭证与目标绑定。** `check_risk_review` 在给定 `target_id` 时要求复核记录的 `target_id` 一致，
  避免「拿 A 的通过结论去下 B 的单」。凭证不是一张万能通行证。

被拒绝时，工具返回的是结构化信封而不是异常穿透：

```json
{"ok": false, "tool": "prepare_order", "blocked_by_gate": true,
 "error": {"code": "RISK_REVIEW_REQUIRED", "message": "...", "hint": "..."}}
```

这让主助手能如实转述「哪道闸、为什么、下一步怎么办」，而不是换一条路绕过去 —— 因为也没有
别的路。

## 7. 回测可信度的三道自检

回测最容易在「看起来很正常」的情况下得出完全错误的结论。本包用三道自检把三类致命偏差挡在
交付之前，同时写进 `strategy-backtest` 技能与 `risk-reviewer` 的复核清单：

1. **前视（look-ahead）**
   - 财务因子只用 `pub_date <= 调仓日` 的财报；
   - 信号 `trade_date` 为生成日，成交必须在**下一交易日**，禁止当日收盘价成交；
   - 复权口径不得混用（复权价配不复权量是典型错误）；
   - **对照实验：把信号整体后移 1 个交易日重跑。若收益由正转负，说明原结论来自未来信息，
     直接判不通过。** 结果写入 `backtests.lag_shift_checked`；未做对照则降档。

2. **幸存者偏差（survivorship）**
   - universe 必须包含 `delist_date` 非空的退市标的；
   - 若用「当前仍在交易」的指数成分近似历史，`get_index_members` 会返回 `point_in_time=false`，
     必须显式声明为缺陷，不能默默当成历史成分。

3. **成本（cost）**
   - 佣金万 2.5（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、滑点必须入模；
   - `backtests.cost_model_json` **缺失即视为未计入成本，风控直接判不通过**；
   - 回测的成本口径与 `gates.estimate_costs` 必须一致，否则回测赚的钱在下单时会被手续费吃掉。

三道自检的落地方式不同：前两道是「技能流程 + 复核提示词 + fixture 回归」，第三道还额外被
`tests/test_order_gate.py` 的费用断言钉死。`tests/test_risk_reviewer_fixtures.py` 用 3 个已知
错误样例（未来函数、无样本外、缺成本模型）+ 1 个干净样例做判定回归 —— 如果三个错误样例里
有一个被判「通过」，说明复核提示词太软，必须先回炉再谈上线。

## 8. 挂载点与文件落位

`install.ps1` 实际做的 9 步与目标位置：

```
quant-agent（源）                                   DeerFlow 实例（目标）
─────────────────────                               ──────────────────────
persona/SOUL.md                                ─►  users/<uid>/agents/quant-agent/SOUL.md
config/agent.config.yaml                       ─►  users/<uid>/agents/quant-agent/config.yaml
skills/<7 个>/（含 templates/）                 ─►  users/<uid>/skills/custom/<name>/
mcp-servers/*                                  ─►  backend/.deer-flow/mcp/quant-agent/
                                                    ├─ quant-data/{server.py, normalize.py}
                                                    └─ trade-gateway/{server.py, gates.py}
（安装器新建）                                  ─►  backend/.deer-flow/mcp/quant-data/.venv
integrations/quant-db/schema.sql + seed.sql     ─►  backend/.deer-flow/data/quant.db
integrations/*/mcp.example.json（3 段合并）      ─►  deer-flow/extensions_config.json
subagents/risk-reviewer.yaml（标记块片段）        ─►  deer-flow/config.yaml 的 subagents.custom_agents
（安装器改写）                                  ─►  config.yaml 的 web_search 后端：tavily → ddg_search
```

安装器的 9 个步骤（与脚本内 `Write-Step` 一一对应）：

1. 复制 Agent（人设 + 配置）与 7 个 Skill；
2. 复制自建 MCP server 到 `backend\.deer-flow\mcp\quant-agent\`；
3. 为 `quant-data` 建独立 venv 并装依赖（fail-soft）；
4. 合并 3 个 MCP 与 7 个 skill 开关到 `extensions_config.json`；
5. 建 `quant.db`（15 表），仅在 `instruments` 为空时灌种子；
6. 把 `risk-reviewer` 片段注入 `config.yaml` 既有 `custom_agents:` 之下；
7. 环境自检（`uv.exe` / `uvx.exe` / QMT 路径 / 8001 端口）；
8. `web_search` 切换为免 Key 的 DuckDuckGo；
9. 收尾提示（重启 Gateway、先跑 data-ops、校验子代理键、卸载方式）。

所有写入都是**幂等**的：技能整目录覆盖；MCP / 技能开关按键合并；子智能体用
`# BEGIN/END quant-agent managed block` 正则先删后插；数据库仅在空库时灌种子。安装前会备份
被改动的 `config.yaml` 与 `extensions_config.json`（同目录 `*.bak_<时间戳>`）。

`-Uninstall` 会移除三个 MCP 键、7 个 skill 开关、managed 块、`agents/quant-agent` 与 7 个技能目录，
但**默认保留** `quant.db`、MCP 代码副本与 venv（避免误删历史成交与持仓）。

## 9. 踩坑记录

### 9.1 Windows 中文系统：MCP 子进程必须强制 UTF-8

现象：纯 ASCII 的返回正常；一旦结果里出现「贵州茅台」「沪深300ETF」或复核结论「通过」，工具调用
就会在 60 秒后超时。

根因：stdio 子进程在中文 Windows 默认按 GBK(cp936) 写 stdout，MCP 客户端按 UTF-8 解码，遇到
非法字节序列后连接卡死。

修复：**三个 MCP 的 `env` 都要写** `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`（自建 server 同样
会踩，不是只有 sqlite 有问题）。`quant-data` 的 `session_init_timeout=180`、`tool_call_timeout=120`
也是为此留的余量（akshare 首次 import 与冷启动慢）。

### 9.2 技能 `allowed-tools` 是精确白名单，YAML 列表不归一化

`SkillToolPolicyMiddleware` 把模型可见工具裁剪为「所有激活技能 `allowed-tools` 的并集」∪
「始终可用内置工具」，并在执行时再次拦截，匹配方式是**工具名精确相等**。

`deerflow/skills/parser.py` 的别名表（`Read → read_file`、`WebSearch → web_search` 等）**只对
「空格分隔字符串」形式生效；写成 YAML 列表时逐项原样保留**。因此列表里写 `Read/Write/WebSearch`
不会匹配到任何运行时工具。本仓库技能统一写运行时真名：

```yaml
allowed-tools:
  - read_file
  - write_file
  - web_search
  - task
  - quant-data_get_daily_bars
  - quant-db_read_query
  - trade-gateway_prepare_order   # 仅 live-trading 持有
```

`task` 不在「始终可用内置工具」内，不显式放行，技能激活后就无法触发子智能体委托 —— 而
`strategy-backtest` / `signal-review` 的复核闭环正是靠它。另外，`check_consistency.py` 会断言
**除 `live-trading` 外任何技能都不得含 `trade-gateway_` 前缀**，保证下单能力只有一个出口。

### 9.3 子代理注入：不能再追加一个顶层 `subagents:`

本机全局 `config.yaml` 已经存在 machine-problems 注入的顶层 `subagents:` 键（含
`custom_agents.safety-reviewer`）。若照旧追加第二个顶层 `subagents:`，会产生**重复键**：YAML
解析失败，或者后一个静默覆盖前一个 —— 两种结果都会让 safety-reviewer 或 risk-reviewer 消失。

改法：`subagents/risk-reviewer.yaml` 写成 **4 空格缩进的片段**（与 `safety-reviewer` 同级），
标记注释放在列 0；安装器用正则定位 `^subagents:\s*$`，再在其后找 `^  custom_agents:\s*$`，
把片段插到该行之后。未命中 `custom_agents` 时补一层，整棵树都不存在时才追加完整的 `subagents:`。

检查方法：

```powershell
uv run --with pyyaml python -c "import yaml;d=yaml.safe_load(open(r'<deer-flow>\config.yaml',encoding='utf-8'));print(list(d['subagents']['custom_agents'].keys()))"
# 应同时看到 safety-reviewer 与 risk-reviewer
```

### 9.4 xtquant 不在 PyPI，用 PYTHONPATH 注入

`xtquant` 随 QMT / MiniQMT 客户端分发，PyPI 上的同名包多为非官方上传。安装器**不执行任何
`pip install`**，而是把 `<QmtPath>\bin.x64\Lib\site-packages` 写进 `trade-gateway` 的 `PYTHONPATH`，
并把 `QUANT_QMT_PATH` 指向 `<QmtPath>\userdata_mini`（`XtQuantTrader` 的第一个参数，不是安装根目录）。

未配置 / 连不上时的行为是**结构化降级**而不是崩溃：`_ensure_trader()` 把原因写进
`_trader_state["error"]`，账户类工具返回 `unavailable`，而 `prepare_order` / `place_order` 的
`dry_run` 通路仍然可用 —— 这正是离线演练与回归测试能跑起来的前提。

### 9.5 附：`quant-db` 必须锁 `mcp==1.2.1`

新版 MCP SDK 移除了旧 sqlite server 依赖的 `Server.list_resources` 装饰器，`uvx mcp-server-sqlite`
会直接起不来。`quant-data` 需要较新的 SDK，所以两套环境必须隔离，不能共用一个 venv。

## 10. 已知限制与一期不做

| 限制 / 不做项 | 原因 |
|---|---|
| 港美股、加密货币 | 监管路径关闭（跨境证券整治、虚拟货币/RWA 相关通知），A 股是唯一能零门槛走通全链路的市场 |
| 高频 / tick 级策略、Level-2 行情 | 数据成本与执行延迟都不在一个「LLM 编排 + 日频」的架构里 |
| 无人值守自动交易 | 一期强制人工贴回 6 位确认码；这是设计选择，不是待办 |
| 融资融券、期权、股指期货 | 需要额外权限门槛，策略与风控模型也不同，后续单独迭代 |
| 历史指数成分股 | 免费源多只提供当前成分，`point_in_time=false`；本包如实标注而不假装拥有 |
| 市价单 | 本期未开放（`MARKET_ORDER_DISABLED`）：无法在下单前校验涨跌停边界 |
| 账户总资产的实时校验 | `check_position_cap` 已实现并被单测覆盖，但下单通路在 `dry_run` / 无 QMT 时拿不到可信总资产，实际以单笔金额上限 `QUANT_MAX_ORDER_AMOUNT` 为主约束 |
| 港股通、北交所细节规则 | 北交所已支持 `board='bse'` 与 ±30%，但做市、盘后固定价格等细则未覆盖 |

## 11. 风险清单

| # | 风险 | 验证方式 | 不通过时的降级方案 |
|---|---|---|---|
| 1 | **xtquant 装不上 / 连不上**（最可能失败） | `python -c "from xtquant.xttrader import XtQuantTrader; from xtquant.xttype import StockAccount"`；再用模拟账号 `XtQuantTrader(path, session_id)` 走 `connect()` + `subscribe()`，确认能收到委托回报 | 降级为 **paper 模式**：`trade-gateway` 完整交付但不接真账户，`order_intents` / `orders` / `trades` 只做本地记录，`get_trading_mode` 返回 `unavailable`，README 明确标注 |
| 2 | **自建 MCP 初启与中文编码** | 重启 Gateway 后调 `quant-data_get_instruments`，返回含中文的标的名不超时；再断开网络确认上游失败返回 `ok:false` 结构化错误而非挂死 | 确认三个 server 都带 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，必要时调大 `session_init_timeout`；完全断网时改用本地 `bars_daily` 缓存查询 |
| 3 | **回测结论不可信**（前视 / 幸存者偏差 / 成本缺失） | 同一策略用 akshare 与 baostock 双源取数对比口径；做「信号后移 1 日」对照；跑 `pytest tests/test_risk_reviewer_fixtures.py`，3 个错误样例必须全部判「不建议执行」 | 复核判「不建议执行」即不交付「可执行」结论；策略只能停在研究与回测阶段，不进入 `signal-review` / `live-trading` |
| 4 | **子代理「必须先复核」只是软约束** | 在会话里诱导主助手跳过 `risk-reviewer` 直接 `prepare_order`，应在 MCP 层收到 `RISK_REVIEW_REQUIRED` 或 `RISK_REVIEW_NOT_FOUND` | 闸 2 已把软约束变成硬拒绝；进一步降级是直接关闭 `trade-gateway`（默认即关闭，`enabled:false`） |
| 5 | **确认码被模型自行填写 / 循环重试** | 检查 `order_intents.confirm_token` 与用户实际输入是否一致；用错误码触发 `TOKEN_MISMATCH`，确认 token 未被消耗 | `consume_intent` 在猜错时不消耗 token；`SKILL.md` 明令禁止编造与重试；`live-trading` 是唯一持有下单工具的技能 |

## 12. 如何用嵌入式客户端自测

不必走 HTTP 鉴权即可在进程内跑通完整链路（DeerFlow 提供 `DeerFlowClient`）：

```python
from deerflow.client import DeerFlowClient

client = DeerFlowClient(
    agent_name="quant-agent",
    subagent_enabled=True,      # 必须开启，否则不挂载 task 工具
    thinking_enabled=True,
)
for ev in client.stream(
    "把 510300.SH 和 600519.SH 今年以来的日线补进库，告诉我覆盖度和复权口径",
    thread_id="smoke-1",
    user_id="<your-deerflow-user-id>",   # 技能/Agent 装在哪个用户空间就传谁
):
    d = ev.data or {}
    if ev.type == "messages-tuple" and d.get("type") == "ai":
        if d.get("content"):
            print(d["content"], end="")
        for tc in d.get("tool_calls") or []:
            print("\n[call]", tc["name"], tc.get("args"))
```

判断要点：`quant-data_*` / `quant-db_*` 返回数据而非 policy/timeout 错误；回测类请求应出现一次
`name == "task"` 的委托，并能在工具结果里看到三档复核结论；下单类请求应先出现
`trade-gateway_get_trading_mode`，再出现 `prepare_order`，且**在用户贴回确认码之前不应出现
`place_order`**。

具体操作步骤见 [`examples/`](../examples/) 下的三个 runbook。
