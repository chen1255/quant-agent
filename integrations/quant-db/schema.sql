-- ============================================================================
-- quant-agent 状态库表结构（quant-db，由 uvx mcp-server-sqlite 挂载）
-- 初始化：scripts/install.ps1 自动执行；手工安装：sqlite3 <db> < schema.sql
--
-- 15 张表，主链路：
--   instruments -> bars_daily / fundamentals -> factor_values -> strategies
--     -> backtests / equity_curve -> signals -> order_intents -> orders -> trades
--   risk_reviews 横向挂回 backtests / signals；positions_daily 为日终快照。
--
-- 设计要点（对应 docs/architecture.md）：
--   1) instruments 保留 delist_date，库里能放已退市标的 —— 治幸存者偏差；
--   2) fundamentals 必带 pub_date（公告日），as-of 查询据此防前视偏差；
--   3) backtests 带 data_snapshot_date + repro_hash —— 回测必须可复现；
--   4) risk_reviews 是下单硬闸门的**唯一**凭证来源（verdict='通过' 才放行）；
--   5) order_intents 记录 6 位确认码的完整生命周期（TTL / 单次消费）；
--   6) 所有 params_json / risk_limits_json / findings_json 统一存 JSON 字符串，
--      便于 scripts/check_consistency.py 做静态校验。
--
-- 单位约定：金额=人民币元；数量=股/份（A股与 ETF 最小 100）；
--           比率类字段一律小数（0.12 表示 12%）；日期 YYYY-MM-DD；时间戳=Unix 秒。
-- ============================================================================

-- 1) 标的台账（含退市，治幸存者偏差）------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
  symbol        TEXT PRIMARY KEY,              -- 统一代码 code.exchange，如 600519.SH / 510300.SH
  code          TEXT NOT NULL,                 -- 纯数字代码 600519（调上游接口用）
  exchange      TEXT NOT NULL CHECK(exchange IN ('SH','SZ','BJ')),
  name          TEXT NOT NULL,                 -- 简称：贵州茅台 / 沪深300ETF
  asset_type    TEXT NOT NULL CHECK(asset_type IN ('stock','etf','index','fund','bond')),
  board         TEXT CHECK(board IN ('main','chinext','star','bse','etf','index','other')),
                                               -- 决定涨跌幅限制：main=±10%、chinext/star=±20%、bse=±30%、etf=±10%
  industry_sw1  TEXT,                          -- 申万一级行业（股票）
  list_date     TEXT,                          -- 上市日 YYYY-MM-DD
  delist_date   TEXT,                          -- 退市日；非空=已退市，必须保留在库里
  lot_size      INTEGER NOT NULL DEFAULT 100,  -- 最小交易单位（股/份）
  is_st         INTEGER NOT NULL DEFAULT 0 CHECK(is_st IN (0,1)),
  index_code    TEXT,                          -- 所属指数（可空，如 000300）
  status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','delisted')),
  updated_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_instruments_type  ON instruments(asset_type, status);
CREATE INDEX IF NOT EXISTS idx_instruments_index ON instruments(index_code);

-- 2) 交易日历 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trading_calendar (
  cal_date     TEXT PRIMARY KEY,               -- YYYY-MM-DD
  exchange     TEXT NOT NULL DEFAULT 'SSE',    -- SSE / SZSE（A股两市日历基本一致）
  is_open      INTEGER NOT NULL CHECK(is_open IN (0,1)),
  prev_open    TEXT,                           -- 上一交易日（便于做 T+1 与信号后移对照）
  note         TEXT                            -- 节假日名，如「国庆节」
);
CREATE INDEX IF NOT EXISTS idx_calendar_open ON trading_calendar(is_open, cal_date);

-- 3) 日线行情（缓存上游，避免重复拉取与限频）-----------------------------------
CREATE TABLE IF NOT EXISTS bars_daily (
  symbol        TEXT NOT NULL,
  trade_date    TEXT NOT NULL,
  open          REAL,
  high          REAL,
  low           REAL,
  close         REAL,
  pre_close     REAL,                          -- 前收，用于计算涨跌停价
  volume        INTEGER,                       -- 股/份
  amount        REAL,                          -- 成交额（元）
  turnover_rate REAL,                          -- 换手率（小数）
  adj_factor    REAL DEFAULT 1.0,              -- 复权因子；price 口径必须与此一致
  adjust_mode   TEXT DEFAULT 'qfq' CHECK(adjust_mode IN ('none','qfq','hfq')),
  source        TEXT,                          -- akshare / baostock / manual
  updated_at    TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars_daily(trade_date);

-- 4) 基本面（pub_date 是防前视的锚点）-----------------------------------------
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol         TEXT NOT NULL,
  report_period  TEXT NOT NULL,                -- 报告期 YYYY-MM-DD（如 2024-09-30）
  pub_date       TEXT NOT NULL,                -- **公告日**：as-of 查询只能看到 pub_date <= as_of 的记录
  pe_ttm         REAL,
  pb             REAL,
  roe            REAL,                         -- 小数
  revenue_yoy    REAL,                         -- 营收同比（小数）
  profit_yoy     REAL,                         -- 净利同比（小数）
  mkt_cap        REAL,                         -- 总市值（元）
  source         TEXT,
  updated_at     TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (symbol, report_period)
);
CREATE INDEX IF NOT EXISTS idx_fundamentals_pub ON fundamentals(symbol, pub_date);

-- 5) 因子定义 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_definitions (
  factor_name   TEXT PRIMARY KEY,              -- 如 momentum_20d
  display_name  TEXT,
  formula       TEXT NOT NULL,                 -- 人类可读公式（实现见 factor-screening 技能）
  direction     TEXT NOT NULL DEFAULT 'higher_better'
                CHECK(direction IN ('higher_better','lower_better','neutral')),
  params_json   TEXT,                          -- {"window":20}
  lookback_days INTEGER,                       -- 需要的历史长度，校验样本是否够
  category      TEXT CHECK(category IN ('momentum','value','quality','growth','volatility','liquidity','sentiment','other')),
  created_at    TEXT DEFAULT (datetime('now','localtime'))
);

-- 6) 因子值 -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factor_values (
  factor_name  TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  trade_date   TEXT NOT NULL,
  raw_value    REAL,
  z_score      REAL,                           -- 横截面标准化（同 trade_date 内）
  pct_rank     REAL,                           -- 横截面分位 0~1
  PRIMARY KEY (factor_name, symbol, trade_date),
  FOREIGN KEY (factor_name) REFERENCES factor_definitions(factor_name)
);
CREATE INDEX IF NOT EXISTS idx_factor_values_date ON factor_values(factor_name, trade_date);

-- 7) 策略定义 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategies (
  strategy_id      TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  description      TEXT,
  universe_json    TEXT NOT NULL,              -- {"asset_types":["etf"],"index_code":"000300","exclude_st":true}
  params_json      TEXT NOT NULL,              -- 策略参数（含默认值）
  risk_limits_json TEXT NOT NULL,              -- {"max_position_pct":0.3,"max_order_amount":100000,"stop_loss":0.08}
  rebalance_rule   TEXT,                       -- 如「每月首个交易日」
  created_at       TEXT DEFAULT (datetime('now','localtime'))
);

-- 8) 回测记录（可复现）--------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtests (
  run_id             TEXT PRIMARY KEY,
  strategy_id        TEXT NOT NULL,
  start_date         TEXT NOT NULL,
  end_date           TEXT NOT NULL,
  sample_type        TEXT NOT NULL DEFAULT 'in'
                     CHECK(sample_type IN ('in','out','walk_forward')),
  cost_model_json    TEXT,                     -- 缺失即视为未计入成本，风控直接判不通过
  benchmark          TEXT,
  annual_return      REAL,
  annual_vol         REAL,
  max_drawdown       REAL,
  max_dd_duration    INTEGER,                  -- 最长回撤持续天数
  sharpe             REAL,
  calmar             REAL,
  win_rate           REAL,
  profit_loss_ratio  REAL,
  turnover           REAL,                     -- 年换手率（倍）
  param_grid_size    INTEGER,                  -- 参数网格规模，用于过拟合评估
  data_snapshot_date TEXT NOT NULL,            -- 数据快照日
  repro_hash         TEXT NOT NULL,            -- 代码+参数+数据快照的哈希
  lag_shift_checked  INTEGER DEFAULT 0 CHECK(lag_shift_checked IN (0,1)),
                                               -- 是否做过「信号后移1日」对照
  notes              TEXT,
  created_at         TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
);

-- 9) 净值曲线 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity_curve (
  run_id     TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  nav        REAL NOT NULL,                    -- 策略净值
  bench_nav  REAL,                             -- 基准净值
  drawdown   REAL,                             -- 当前回撤（负数或 0）
  PRIMARY KEY (run_id, trade_date),
  FOREIGN KEY (run_id) REFERENCES backtests(run_id)
);

-- 10) 信号 --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
  signal_id      TEXT PRIMARY KEY,
  strategy_id    TEXT,
  run_id         TEXT,                         -- 来源回测（可空=实盘盘中生成）
  trade_date     TEXT NOT NULL,                -- 信号生成日（成交必须在**下一交易日**）
  symbol         TEXT NOT NULL,
  side           TEXT NOT NULL CHECK(side IN ('buy','sell')),
  score          REAL,
  target_weight  REAL,
  ref_price      REAL,                         -- 信号参考价
  status         TEXT NOT NULL DEFAULT 'new'
                 CHECK(status IN ('new','reviewed','approved','rejected','executed','expired')),
  risk_review_id TEXT,                         -- 通过复核后回填，是下单前置条件之一
  note           TEXT,
  created_at     TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (risk_review_id) REFERENCES risk_reviews(review_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(trade_date, status);

-- 11) 风控复核记录（下单硬闸门的凭证来源）-------------------------------------
CREATE TABLE IF NOT EXISTS risk_reviews (
  review_id      TEXT PRIMARY KEY,
  target_type    TEXT NOT NULL CHECK(target_type IN ('strategy','backtest','signal','order')),
  target_id      TEXT NOT NULL,                -- 指向 backtests.run_id / signals.signal_id 等
  reviewer       TEXT NOT NULL DEFAULT 'risk-reviewer',
  verdict        TEXT NOT NULL
                 CHECK(verdict IN ('通过','补充措施后可执行','不建议执行')),
  findings_json  TEXT,                         -- [{"dimension":"前视偏差","evidence":"...","verdict":"...","suggestion":"..."}]
  lag_shift_test TEXT,                         -- 信号后移对照测试结论
  reviewed_at    INTEGER,                      -- Unix 秒
  created_at     TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_risk_reviews_target ON risk_reviews(target_type, target_id);

-- 12) 下单意图与确认码（人工闸门）---------------------------------------------
CREATE TABLE IF NOT EXISTS order_intents (
  intent_id      TEXT PRIMARY KEY,
  created_at     INTEGER NOT NULL,             -- Unix 秒
  expires_at     INTEGER NOT NULL,             -- 确认码过期时刻（默认 created_at + 120）
  consumed_at    INTEGER,                      -- 首次消费时刻；非空即不可复用
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','consumed','expired','rejected')),
  symbol         TEXT NOT NULL,
  side           TEXT NOT NULL CHECK(side IN ('buy','sell')),
  order_type     TEXT NOT NULL DEFAULT 'limit' CHECK(order_type IN ('limit','market')),
  price          REAL,
  qty            INTEGER NOT NULL,
  est_amount     REAL,
  strategy_id    TEXT,
  signal_id      TEXT,
  risk_review_id TEXT NOT NULL,                -- 生成意图时已校验 verdict='通过'
  confirm_token  TEXT NOT NULL,               -- 6 位数字，2 分钟 TTL，单次消费
  account_id     TEXT,
  payload_json   TEXT,                         -- 原始请求快照，便于审计与重放比对
  reject_reason  TEXT,
  FOREIGN KEY (risk_review_id) REFERENCES risk_reviews(review_id)
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON order_intents(status, expires_at);

-- 13) 订单 --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
  order_id        TEXT PRIMARY KEY,
  intent_id       TEXT,
  signal_id       TEXT,
  strategy_id     TEXT,
  account_id      TEXT,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL CHECK(side IN ('buy','sell')),
  order_type      TEXT NOT NULL DEFAULT 'limit' CHECK(order_type IN ('limit','market')),
  price           REAL,
  qty             INTEGER NOT NULL,
  filled_qty      INTEGER DEFAULT 0,
  avg_fill_price  REAL,
  fee             REAL DEFAULT 0,              -- 佣金（含最低 5 元）
  tax             REAL DEFAULT 0,              -- 印花税（仅卖出）
  transfer_fee    REAL DEFAULT 0,              -- 过户费
  status          TEXT NOT NULL DEFAULT 'new'
                  CHECK(status IN ('new','dry_run','submitted','partial','filled','cancelled','rejected','failed')),
  is_dry_run      INTEGER NOT NULL DEFAULT 1 CHECK(is_dry_run IN (0,1)),
  xt_order_id     TEXT,                        -- QMT/xtquant 返回的委托编号
  reject_reason   TEXT,
  submitted_at    INTEGER,                     -- Unix 秒
  updated_at      TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (intent_id) REFERENCES order_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, symbol);

-- 14) 成交明细 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
  trade_id   TEXT PRIMARY KEY,
  order_id   TEXT NOT NULL,
  xt_trade_id TEXT,
  symbol     TEXT NOT NULL,
  side       TEXT NOT NULL CHECK(side IN ('buy','sell')),
  price      REAL NOT NULL,
  qty        INTEGER NOT NULL,
  amount     REAL,
  fee        REAL DEFAULT 0,
  tax        REAL DEFAULT 0,
  trade_time TEXT,                             -- YYYY-MM-DD HH:MM:SS
  created_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

-- 15) 日终持仓快照 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions_daily (
  account_id   TEXT NOT NULL,
  trade_date   TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  qty          INTEGER NOT NULL DEFAULT 0,     -- 持仓数量
  avail_qty    INTEGER NOT NULL DEFAULT 0,     -- 可卖数量（T+1 约束）
  avg_cost     REAL,                           -- 持仓成本价
  last_price   REAL,
  market_value REAL,
  unrealized_pnl REAL,
  weight       REAL,                           -- 占总资产比例
  PRIMARY KEY (account_id, trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_positions_date ON positions_daily(trade_date);

-- ============================================================================
-- 常用检索（Agent 经 quant-db read_query 自主执行）
-- ============================================================================
-- 全市场标的池：SELECT symbol,name,asset_type,board,lot_size FROM instruments
--               WHERE status='active' AND asset_type IN ('stock','etf') ORDER BY symbol;
-- 含退市标的的完整宇宙（幸存者偏差自检）：
--   SELECT COUNT(*) total, SUM(CASE WHEN delist_date IS NOT NULL THEN 1 ELSE 0 END) delisted FROM instruments;
-- as-of 基本面（防前视）：SELECT symbol,report_period,pub_date,roe,pe_ttm FROM fundamentals
--   WHERE symbol='600519' AND pub_date <= '2024-10-15' ORDER BY pub_date DESC LIMIT 5;
-- 最近交易日：SELECT cal_date FROM trading_calendar WHERE is_open=1 AND cal_date <= date('now') ORDER BY cal_date DESC LIMIT 1;
-- 信号待复核：SELECT signal_id,trade_date,symbol,side,score,status FROM signals WHERE status IN ('new','reviewed') ORDER BY trade_date DESC;
-- 复核结论：SELECT review_id,target_type,target_id,verdict,reviewed_at FROM risk_reviews ORDER BY reviewed_at DESC LIMIT 20;
-- 未消费且未过期的下单意图：SELECT intent_id,symbol,side,qty,expires_at FROM order_intents WHERE status='pending';
-- 今日成交与费用：SELECT o.symbol,o.side,o.qty,o.avg_fill_price,o.fee+o.tax+o.transfer_fee cost FROM orders o
--   WHERE date(o.updated_at)=date('now') AND o.status='filled';
-- 当前模拟持仓权重：SELECT symbol,market_value,weight FROM positions_daily
--   WHERE account_id='SIM-001' AND trade_date=(SELECT MAX(trade_date) FROM positions_daily);
