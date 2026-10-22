# quant-agent · A Quantitative Trading Agent for A-shares & ETFs

A quant trading agent for **independent quant researchers and semi-automated retail traders**: it turns
"I want to validate a strategy" into one **auditable, reproducible, independently risk-reviewed**
pipeline — ingest market data → research the sector → screen factors → backtest → review signals →
set risk limits → place constrained orders. Before real money moves, the work has to clear an
independent risk review and the local order gates.

> This is an **industry agent asset pack built on top of** [DeerFlow](https://github.com/bytedance/deer-flow)
> (ByteDance's open-source agent framework built on LangGraph).
> This repository **does not contain and is not** the DeerFlow framework itself — it is an original set of
> persona, skills, domain schema and sub-agent configuration that mounts onto an existing DeerFlow
> instance with a single script. The runtime, model calls, tooling and web UI are all provided by DeerFlow.
> DeerFlow is optional, though: [`cli/quantctl.py`](cli/quantctl.py) runs the same assets as a standalone CLI.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What it does

| Capability | Description |
|---|---|
| 🗄️ Data operations | Checks local coverage first, backfills only the gaps, then writes into `quant-db`; symbols normalized to `code.exchange`, adjustment mode stated explicitly, delisted names kept, fundamentals always carry their publication date |
| 🔍 Market research | Fundamental and price/volume research on sectors, single names and ETFs — conclusion first, evidence traceable, counter-evidence and uncertainty included; never emits an order |
| 🧮 Factor screening | Cross-sectional scoring and ranking over a given universe, producing a candidate pool and writing `factor_values` (and `signals` when needed); does not evaluate historical returns |
| 📈 Strategy backtest | Cost model, data snapshot date and reproduction hash are mandatory; a one-day signal-lag control run and out-of-sample evidence are enforced; results must pass `risk-reviewer` |
| 📋 Signal review | Reads `signals`, verifies tradability (suspension / limit-up-down / one-word board / T+1 / round lot), checks against risk limits and drafts a rebalance plan; **never places orders** |
| 🛡️ Risk limits | Maintains `strategies.risk_limits_json` and quantifies single-name and SW-level-1 concentration plus liquidity constraints; **never offers shortcuts to loosen risk controls or add leverage** |
| 🔐 Constrained execution | The only skill holding `trade-gateway_*` tools; enforces "check mode → verify credential → get token → user pastes it back → place order" with four code-level gates |

## Flagship feature 1: the risk-review sub-agent

The lead agent collaborates with a `risk-reviewer` sub-agent that is **read-only and cannot delegate
further**. It is not a decorative "are you sure?" step — its verdict is the sole source of the order
credential:

```
User question / backtest deliverable / rebalance plan
        │
        ▼
quant-agent (lead agent: 7 skills + 3 MCP servers, temperature=0.2)
        │  a backtest is about to be delivered, or a plan is about to reach order placement
        │  delegates through the task tool (allowlist: risk-reviewer only)
        ▼
risk-reviewer (senior quant risk analyst, read-only recomputation)
        │  tools: read_file + quant-db_list_tables / describe_table / read_query
        │  disallowed_tools: task / write_file / quant-db_write_query
        │  checks 11 dimensions; audits risk only, never rewrites the strategy
        ▼
Three verdicts: 通过 (pass) / 补充措施后可执行 (pass with conditions) / 不建议执行 (do not execute)
        │
        ▼
The lead agent writes the verdict into risk_reviews via quant-db_write_query
(review_id + verdict + findings_json + lag_shift_test)
        │
        ▼
trade-gateway_prepare_order reads that table
        │  executable tier → issues a 6-digit confirmation token
        │  otherwise       → rejects outright, no token issued
```

The 11 review dimensions: look-ahead bias, survivorship bias, overfitting, in-sample vs out-of-sample,
performance, concentration, liquidity, tradability, transaction costs, leverage & funding, red lines.

**The review verdict is a hard credential, not decoration.** It is genuinely written into the
`risk_reviews` table, and `trade-gateway_prepare_order` genuinely queries that table. A missing
`risk_review_id`, a non-existent record, a verdict outside the executable tier, or a credential that
points at a different symbol than the one being ordered all fail to produce a confirmation token.
The sub-agent itself holds no write-capable tool, so "issuing myself a pass" is blocked in code.

## Flagship feature 2: the four order gates

| # | Gate | Where it lives | What it enforces |
|---|---|---|---|
| 1 | **Off by default** | `extensions_config.json` + env vars | The MCP entry ships with `enabled:false`; only `install.ps1 -EnableTrading` writes `true`; `QUANT_TRADE_DRY_RUN` defaults to `1`, so even when enabled it only writes the local `orders` table and never reaches the broker |
| 2 | **Risk-review credential** | `gates.check_risk_review` | `prepare_order` requires a `risk_review_id` whose record actually exists and whose `verdict` is in the executable tier (`通过` / `补充措施后可执行`); `不建议执行` is rejected outright. When a `signal_id` is supplied, the credential must target the same signal (no "review A, order B") |
| 3 | **Six-digit human confirmation token** | `gates.create_intent` / `consume_intent` | `prepare_order` returns an `intent_id` plus a 6-digit `confirm_token` with a 120-second TTL, single-use, with its full lifecycle persisted in `order_intents`. The token must be pasted back by the user; the model may neither invent it nor retry in a loop |
| 4 | **Pre-trade re-validation** | `gates.consume_intent` + `check_limits` | `place_order` re-checks token freshness and consumption state, the risk credential, the per-order amount cap, round-lot multiples, limit-up/limit-down boundaries and suspension/delisting. Under `dry_run=1` it only writes `orders`, and `xt_order_id` must be null |

**The gates live in the MCP layer, not in the prompt layer.** A prompt that says "always review
first" or "never place orders on your own" can be talked around — rephrasing, multi-turn nudging, or
simply skipping a skill can make a soft constraint evaporate. What actually stops the agent is the
Python branching and the `GateReject` error codes inside `prepare_order` / `place_order`: they do not
understand "the model thinks it's fine", they only understand rows in `risk_reviews` and state
transitions in `order_intents`.

## Domain knowledge base (quant-db, 15 related tables)

A local SQLite database mounted through `uvx --with mcp==1.2.1 mcp-server-sqlite`, with every tool
exposed under the `quant-db_` prefix. The main chain:

```
instruments ─┬─► bars_daily ─┐
             ├─► fundamentals ┼─► factor_values ─► strategies
             └─► trading_calendar                    │
                                                     ├─► backtests ─► equity_curve
                                                     │        │
                                                     │        ▼
                                                     └─► signals
                                                              │
                            risk_reviews ◄──── verdict persisted ─┤
                                                              ▼
                                                    order_intents (confirmation token)
                                                              │
                                                           orders ─► trades
                                                              │
                                                     positions_daily (end-of-day snapshot)
```

| Table | Key columns | Design intent |
|---|---|---|
| `instruments` | `symbol` PK, `exchange`, `asset_type`, `board`, `industry_sw1`, `list_date`, `delist_date`, `lot_size`, `is_st`, `index_code`, `status` | **Keeps delisted names (`delist_date` not null) to cure survivorship bias**; `board` determines the price limit (main ±10%, ChiNext/STAR ±20%, BSE ±30%, ETF ±10%); `lot_size` is **200** for STAR Market |
| `trading_calendar` | `cal_date` PK, `is_open`, `prev_open`, `note` | `prev_open` underpins T+1 execution and the signal-lag control run |
| `bars_daily` | `symbol`+`trade_date` PK, OHLC, `pre_close`, `volume`, `amount`, `turnover_rate`, `adj_factor`, `adjust_mode`, `source` | Prices carry a **cumulative adjustment factor**; `volume`/`amount` are always unadjusted raw values; `source` records akshare / baostock / manual |
| `fundamentals` | `symbol`+`report_period` PK, **`pub_date`**, `pe_ttm`, `pb`, `roe`, `revenue_yoy`, `profit_yoy`, `mkt_cap` | **`pub_date` is the anchor against look-ahead bias**: as-of queries may only see filings with `pub_date <= as_of`; records without a publication date are dropped |
| `factor_definitions` | `factor_name` PK, `formula`, `direction`, `params_json`, `lookback_days`, `category` | Formulas and directions are persisted, never recalled from memory |
| `factor_values` | `factor_name`+`symbol`+`trade_date` PK, `raw_value`, `z_score`, `pct_rank` | Cross-sectional standardization happens within a single `trade_date` only |
| `strategies` | `strategy_id` PK, `universe_json`, `params_json`, `risk_limits_json`, `rebalance_rule` | Parameters and risk limits live in the same source of truth |
| `backtests` | `run_id` PK, `cost_model_json`, `param_grid_size`, **`data_snapshot_date`**, **`repro_hash`**, **`lag_shift_checked`** | **Reproducibility**: snapshot date plus a hash of code/params/data. Missing lag-shift check downgrades the verdict; missing cost model fails it |
| `equity_curve` | `run_id`+`trade_date` PK, `nav`, `bench_nav`, `drawdown` | Strategy and benchmark net value aligned per day; drawdown left as a trail |
| `signals` | `signal_id` PK, `trade_date`, `symbol`, `side`, `score`, `target_weight`, `status`, `risk_review_id` | `trade_date` is the **signal generation date**; execution must be the next trading day. `status` moves new → reviewed → approved |
| `risk_reviews` | `review_id` PK, `target_type`, `target_id`, `verdict`, `findings_json`, `lag_shift_test` | **The single source of the order gate credential** |
| `order_intents` | `intent_id` PK, `expires_at`, `consumed_at`, `status`, `confirm_token`, `risk_review_id` | Full token lifecycle: TTL, single use, rejection reason |
| `orders` | `order_id` PK, `intent_id`, `status`, `is_dry_run`, `xt_order_id`, `fee`, `tax`, `transfer_fee` | A dry-run order must be `status='dry_run'` with a null `xt_order_id` |
| `trades` | `trade_id` PK, `order_id`, `price`, `qty`, `fee`, `tax`, `trade_time` | Fill details; `xt_trade_id` reconciles with broker reports |
| `positions_daily` | `account_id`+`trade_date`+`symbol` PK, `qty`, `avail_qty`, `avg_cost`, `market_value`, `weight` | End-of-day snapshot; `avail_qty` reflects the T+1 sellable constraint |

Units: amounts in CNY; quantities in shares/units; all ratios as decimals (`0.12` means 12%); dates as
`YYYY-MM-DD`; timestamps as Unix seconds.

## Data conventions (read this first)

Get the conventions wrong and every downstream factor, backtest and review is wrong — the most
expensive class of bug in quant work.

- **Price adjustment**: `get_daily_bars` accepts `adjust` ∈ `none` / `qfq` / `hfq`, default `qfq`.
  `adj_factor` in the database is always the **cumulative factor (back-adjusted basis)**;
  `qfq = price × adj_factor / latest_factor`.
- **Volume is always unadjusted**: `volume` / `amount` remain raw, unadjusted values. **Never apply an
  adjustment factor to volume**, and declare one price basis (`none` / `qfq` / `hfq`) per calculation
  instead of switching mid-way.
- **Fundamentals require `as_of_date`**: the tool returns only records with `pub_date <= as_of_date`
  and drops those without a publication date, reporting them in `warnings`. Fetching historical
  fundamentals without `as_of_date` is look-ahead bias by definition.
- **T+1 execution**: `signals.trade_date` is the generation date; execution must happen on the **next
  trading day**. Backtests may not fill at the same day's close.
- **Round lots**: 100 shares/units for A-shares and ETFs; **200 for STAR Market names (`688` prefix)**,
  as recorded in `instruments.lot_size`.
- **Cost model**: commission 0.025% with a **5 CNY minimum**, stamp duty **0.05% on sells only**,
  transfer fee **0.001%**, plus slippage. `backtests.cost_model_json` and `gates.estimate_costs` must
  agree.
- **Index constituents**: free data sources mostly provide only current membership; `get_index_members`
  reports `point_in_time=false` honestly, and backtests must treat that as a source of constituent
  look-ahead.
- **Untradable when unverifiable**: if `get_limit_status` cannot obtain the previous close, or returns
  `tradable=false`, treat the name as untradable.

## Repository layout

```
quant-agent/
├─ README.md                            # Chinese primary README
├─ README_EN.md                         # This file
├─ LICENSE                              # MIT + investment-risk notice
├─ .gitignore                           # excludes *.db / venv / machine-private config
├─ persona/SOUL.md                      # Persona: role, convention discipline, review rules, disclaimer
├─ config/agent.config.yaml             # Agent config (7 skills / 3 MCP servers / sub-agent allowlist / temperature=0.2)
├─ skills/                              # 7 domain skills (SKILL.md + fill-in templates)
│  ├─ data-ops/                         #   ingestion (templates/dataset_template.md)
│  ├─ market-research/                  #   sector & name research (templates/research_template.md)
│  ├─ factor-screening/                 #   factor scoring & candidate pool (templates/screening_template.md)
│  ├─ strategy-backtest/                #   backtesting & performance (templates/backtest_template.md)
│  ├─ signal-review/                    #   signal review & execution plan (templates/execution_template.md)
│  ├─ live-trading/                     #   constrained order placement (templates/order_template.md)
│  └─ risk-limits/                      #   risk parameters & limits (templates/risk_limits_template.md)
├─ mcp-servers/                         # 2 custom Python stdio MCP servers
│  ├─ quant-data/                       #   server.py + normalize.py (conventions & error envelope)
│  └─ trade-gateway/                    #   server.py + gates.py (the four gates as pure functions)
├─ integrations/
│  ├─ quant-data/mcp.example.json       # data MCP snippet (includes UTF-8 settings)
│  ├─ quant-db/
│  │  ├─ schema.sql                     # 15-table DDL + common queries
│  │  ├─ seed.sql                       # demo seed (contains no prices or financial values)
│  │  └─ mcp.example.json               # state DB MCP snippet
│  └─ trade-gateway/mcp.example.json    # trading snippet (enabled:false + xtquant via PYTHONPATH)
├─ subagents/risk-reviewer.yaml         # risk-review sub-agent (4-space indented fragment + idempotent markers)
├─ cli/                                 # standalone entry point (no DeerFlow required)
│  ├─ quantctl.py                       #   MCP bridge + tool loop + skill narrowing + review delegation
│  ├─ config.example.json               #   template (use config.local.json for machine paths)
│  └─ requirements.txt                  #   mcp / httpx
├─ scripts/
│  ├─ install.ps1                       # one-click Windows installer (9 steps, idempotent)
│  └─ check_consistency.py              # cross-file consistency check (offline, 8 categories)
├─ tests/                               # pytest (offline; no network, no QMT)
│  ├─ conftest.py                       #   builds a DB from the real schema.sql; loads modules by path
│  ├─ test_order_gate.py                #   rejection paths of the four gates
│  ├─ test_data_ops.py                  #   symbol/board/price-limit/adjustment/error-envelope checks
│  ├─ test_risk_reviewer_fixtures.py    #   prompt coverage + 3-bad/1-good verdict regression
│  └─ fixtures/risk_reviewer/           #   clean / future_function / missing_cost_model / no_out_of_sample
├─ examples/                            # 3 reproducible runbooks (not fabricated transcripts)
│  ├─ 01-data-ops-runbook.md
│  ├─ 02-backtest-with-risk-review.md
│  └─ 03-dry-run-order-two-step.md
└─ docs/architecture.md                 # architecture, gate design, mount points, field notes
```

## Requirements

- A working [DeerFlow](https://github.com/bytedance/deer-flow) instance (validated against the main
  branch backend + web gateway)
- Python 3.12 and [uv/uvx](https://docs.astral.sh/uv/) (used to launch MCP servers and to create an
  isolated venv)
- An LLM API key (any DeerFlow-supported model; **no** dedicated web-search key is needed — the
  installer points `web_search` at keyless DuckDuckGo by default)
- Optional for live trading: a QMT / MiniQMT client (xtquant ships with the client and is **not on
  PyPI**) plus quant-trading permission from your broker
- Windows / macOS / Linux all work; the installer and troubleshooting notes target **Windows with a
  Chinese locale**

## Installation

### One-click on Windows (recommended)

Run in a normal PowerShell window (the script defaults to `d:\...\deepflow\deer-flow`; override with
`-DeerFlowRoot`):

```powershell
.\scripts\install.ps1
# or target a different instance / user:
.\scripts\install.ps1 -DeerFlowRoot "D:\path\to\deer-flow" -UserId "<your-user-id>"
# enable the trading gateway (still dry-run by default, no broker orders):
.\scripts\install.ps1 -EnableTrading -QmtPath "C:\国金QMT" -QmtAccount "your-fund-account"
# uninstall (keeps quant.db and the venv):
.\scripts\install.ps1 -Uninstall
```

The installer idempotently performs **9 steps**:

1. Copy the agent (`SOUL.md` + `config.yaml`) and all 7 skills into the user space under
   `agents/quant-agent/` and `skills/custom/`;
2. Copy `mcp-servers/*` into `<deer-flow>\backend\.deer-flow\mcp\quant-agent\` so the install no longer
   depends on this repository's location;
3. Create a dedicated venv for `quant-data` and install `akshare` / `baostock` / `mcp>=1.28,<2`
   (isolated from machine-db's pinned `mcp==1.2.1`; this is the slow networked step and fails soft);
4. Merge the `quant-data` / `quant-db` / `trade-gateway` MCP entries and the 7 skill switches into
   `extensions_config.json` (absolute paths written; `trade-gateway` keeps `enabled:false`);
5. Create `quant.db` with its 15 tables and load the demo seed **only when `instruments` is empty**;
6. Inject `risk-reviewer` as an idempotent `# BEGIN/END quant-agent managed block`, **under the
   existing `custom_agents:` key** rather than adding a second top-level `subagents:` key;
7. Run environment self-checks (`uv.exe` / `uvx.exe` / QMT path / port 8001);
8. Switch `web_search` from tavily (needs a key) to DuckDuckGo (`-UseDuckDuckGo:$false` keeps the
   original backend);
9. Print the follow-ups: restart the Gateway, run data-ops once, verify the sub-agent keys, and how to
   uninstall.

Available parameters: `-DeerFlowRoot`, `-UserId`, `-EnableTrading`, `-QmtPath`, `-QmtAccount`, `-UseDuckDuckGo`,
`-SkipSubagent` (debugging only), `-Uninstall`.

Then **restart the Gateway** (configuration is not hot-reloaded) and pick **"量化交易助手"** in a new
session. Modified `config.yaml` and `extensions_config.json` are backed up next to the originals as
`*.bak_<timestamp>`.

### Manual installation

1. Put `persona/SOUL.md` and `config/agent.config.yaml` into `agents/quant-agent/` in the user space
   (rename the config to `config.yaml`);
2. Copy the `skills/*` directories into `skills/custom/`;
3. Copy `mcp-servers/` into `<deer-flow>\backend\.deer-flow\mcp\quant-agent\`;
4. Following the comments in the three `integrations/*/mcp.example.json` files, merge the `mcpServers`
   and `skills` blocks into `deer-flow/extensions_config.json`, replacing `command`, `QUANT_DB_PATH`
   and `PYTHONPATH` with absolute local paths;
5. `sqlite3 <db> < integrations/quant-db/schema.sql`, then load `seed.sql` if the database is empty;
6. Insert the **4-space indented fragment** between the markers in `subagents/risk-reviewer.yaml` under
   the existing `subagents.custom_agents:` key in `config.yaml`.

See [docs/architecture.md](docs/architecture.md) for the mount points in detail.

## Running without DeerFlow

DeerFlow supplies orchestration, UI and login, but it is **not a runtime prerequisite**. The three MCP
servers are plain stdio processes, and `cli/quantctl.py` mounts them directly against the same assets:

```powershell
# 1. Dependencies (mcp + httpx)
pip install -r cli/requirements.txt

# 2. Copy the template and fill in absolute local paths
copy cli\config.example.json cli\config.local.json

# 3. Self-check: MCP connections / skills & sub-agent / model reachability
python cli\quantctl.py --config cli\config.local.json doctor

# 4. One-shot or interactive
python cli\quantctl.py --config cli\config.local.json run "Show how far the local 510300 daily bars go"
python cli\quantctl.py --config cli\config.local.json chat
```

Behaviour mirrors the DeerFlow setup:

| Piece | How quantctl does it |
|---|---|
| Persona | `persona/SOUL.md` becomes the system prompt |
| Skills | Only `name + description` are in the prompt; the model calls `describe_skill` to load the full text, and the toolset is then **narrowed to that skill's `allowed-tools`** |
| Data / state DB / execution | The three MCP servers are mounted over stdio with server-prefixed tool names (`quant-data_*`, `quant-db_*`, `trade-gateway_*`) |
| Risk review | The `task` tool only accepts `risk-reviewer`; its prompt comes from `subagents/risk-reviewer.yaml` and its tools are limited to `read_file` plus the three read-only `quant-db` tools |
| Four gates | Live entirely in the MCP code layer; quantctl neither bypasses nor reimplements them |

`cli/config.local.json` is covered by `.gitignore` (machine paths) — do not commit it. The default model
is `deepseek-v4-pro` with the key read from `DEEPSEEK_API_KEY`; point `base_url` and `model` at any
OpenAI-compatible endpoint to switch.

## Automated checks and tests

Two commands, both **offline** — no network, no QMT, no model required:

```powershell
python scripts/check_consistency.py    # cross-file consistency (8 categories)
python -m pytest tests -q              # unit and regression tests
```

`check_consistency.py` verifies eight categories: (1) every skill / MCP / sub-agent referenced in the
config has a matching file; (2) every `allowed-tools` entry exists in the declared tool inventory;
(3) **no skill other than `live-trading` may reference a `trade-gateway_` tool**; (4) the tables
created by `schema.sql` cover every table referenced by skills and MCP code; (5) every repository path
referenced by `install.ps1` exists and the switches/defaults are correct; (6) `trade-gateway` is off by
default in both the example config and the installer; (7) `risk-reviewer.yaml` is an indented fragment,
its tools are declared, and it is denied write and delegation; (8) `schema.sql` creates exactly 15
tables and all deliverables are present.

The **88 tests** in `tests/` cover three areas:

- **Order gates** (`test_order_gate.py`): the five required rejection paths — expired token, repeated
  consumption, missing `risk_review_id`, non-executable verdict, and dry-run never reaching the broker
  — plus wrong-token-does-not-consume, sweeping of expired intents, per-order cap / round lot /
  suspension / price-limit breaches, the position cap and the cost model.
- **Convention normalization** (`test_data_ops.py`): symbol normalization (including `000001`, which
  exists on both exchanges), board inference, price-limit percentages, cent-level rounding of limit
  prices, price adjustment, upstream row normalization for Chinese and English columns, the structured
  error envelope and the stability of rejection codes.
- **Risk review** (`test_risk_reviewer_fixtures.py`): asserts the prompt covers all 11 dimensions and
  three verdicts, requires the "shift all signals by one trading day" control and a hard fail when
  `cost_model_json` is missing, then runs 4 fixtures (**3 bad, 1 good**) to prove the hard criteria
  cannot let a known-bad case pass.

## Verified locally

Only reproducible engineering results are reported here:

- `python -m pytest tests -q` → **88 passed**;
- `python scripts/check_consistency.py` → all checks pass;
- both custom MCP servers start under an MCP client, and results containing Chinese (for example
  "沪深300ETF" or the verdict "通过") return correctly with `PYTHONUTF8=1` instead of timing out after
  60 seconds;
- under `dry_run=1`, `trade-gateway` completes the full `prepare_order` → user pastes the token back →
  `place_order` flow, producing an `orders` row with `status='dry_run'`, a null `xt_order_id`, and no
  broker order.

> No backtest returns, Sharpe ratios, drawdowns or market figures are published here — those have to be
> produced from your own data. Models differ in how reliably they call tools and trigger delegation;
> validate against your own data and policies before production use.

The three files in [`examples/`](examples/) are **reproducible runbooks**, not fabricated transcripts or
backtest results: `01` builds a universe and backfills daily bars while checking coverage and
conventions; `02` shows how an ETF momentum rotation backtest is persisted and sent to `risk-reviewer`;
`03` walks through the two-step dry-run order flow and the rejection codes.

## Safety and disclaimer (please read)

- This project is **a quantitative research and automation tool, not investment advice**. `SOUL.md`
  explicitly forbids promises like "guaranteed profit" or "must go up", and forbids inventing market
  data, returns, factor IC values or backtest results.
- **Historical backtests do not predict future returns.** All live P&L and any consequences are borne
  by the user.
- The trading gateway is **disabled by default** (`enabled:false`), and even when enabled it keeps
  `QUANT_TRADE_DRY_RUN=1`, writing only the local `orders` table. Real order submission requires
  explicitly setting `QUANT_TRADE_DRY_RUN=0` and confirming that you have obtained quant-trading
  permission from your broker and signed the relevant agreements.
- The demo seed (`seed.sql`) contains only public facts — codes, names, boards, industries. It carries
  **no prices and no financial values**; `backtests`, `signals`, `orders` and similar tables are
  deliberately left empty and must be filled by real workflows.
- Released under MIT; DeerFlow itself remains under its own license.

## Windows / Chinese-locale troubleshooting

- **Symptom**: MCP tools work for pure-ASCII results but time out after 60 seconds as soon as a result
  contains Chinese.
  **Cause**: stdio child processes on a Chinese Windows default to GBK (cp936) on stdout, while the MCP
  client decodes as UTF-8.
  **Fix**: every one of the three MCP servers needs `PYTHONUTF8=1` in `env` (plus
  `PYTHONIOENCODING=utf-8` where possible) — **not just the sqlite one**. The installer injects this;
  do not omit it in a manual install.
- **Symptom**: after a skill loads, MCP or `task` tools report "not allowed by the active skill policy".
  **Cause**: `allowed-tools` is an exact allowlist, and **YAML list form does not normalize aliases**.
  Writing `Read/Write/WebSearch` matches no runtime tool.
  **Fix**: always use the runtime names — `read_file`, `write_file`, `web_search`, `task`,
  `quant-data_get_daily_bars`, `quant-db_read_query`, `trade-gateway_prepare_order`.
- **Symptom**: the `quant-db` MCP server fails to start or exposes no tools.
  **Cause**: newer MCP SDKs removed the `Server.list_resources` decorator that the legacy sqlite server
  depends on.
  **Fix**: pin `uvx --with mcp==1.2.1 mcp-server-sqlite`; `quant-data` uses its own venv with
  `mcp>=1.28,<2`. The two environments stay isolated.
- **Symptom**: after installing `risk-reviewer`, `config.yaml` fails to parse, or the existing
  `safety-reviewer` disappears.
  **Cause**: the global `config.yaml` already has a top-level `subagents:` key injected by
  machine-problems; adding a second one duplicates the key (parse failure or silent overwrite).
  **Fix**: `subagents/risk-reviewer.yaml` is a **4-space indented fragment** and must be inserted right
  after the existing `custom_agents:` line (the installer does this). Verify with:

  ```powershell
  uv run --with pyyaml python -c "import yaml;d=yaml.safe_load(open(r'<deer-flow>\config.yaml',encoding='utf-8'));print(list(d['subagents']['custom_agents'].keys()))"
  # you should see both safety-reviewer and risk-reviewer
  ```

  > Note: `risk-reviewer` is a custom `custom_agents` entry, and `allowed_subagents` authorizes only
  > this one — the lead agent cannot see or invoke any other sub-agent.
- **Symptom**: the first call to any `quant-data` data tool (calendar, daily bars, fundamentals)
  stalls for 60–100 seconds and times out, while `python -c "import akshare"` on the same machine
  takes about 2 seconds. **Cause**: the first import of akshare inside an MCP worker thread is
  abnormally slow because of its dependency `numpy._core._multiarray_umath` (measured 76–100 s),
  especially in a Python 3.14 venv. **Fix**: pin the venv to Python 3.12 (the installer uses
  `uv venv --python 3.12`), and `server.py` pre-imports `akshare` / `baostock` on the main thread
  before `mcp.run()`, moving that cost into session initialization (`session_init_timeout` is 180 s).
- **xtquant is not on PyPI**: it ships with the QMT client, and same-named PyPI packages are mostly
  unofficial uploads. The installer performs no `pip install`; it injects
  `<QmtPath>\bin.x64\Lib\site-packages` through `PYTHONPATH`.

## License

[MIT](LICENSE) © 2026 chen1255
