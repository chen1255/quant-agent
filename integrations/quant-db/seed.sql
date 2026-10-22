-- ============================================================================
-- quant-db 演示种子数据（仅在空库时灌入，见 scripts/install.ps1）
--
-- 【数据诚实性声明】
--   * 下列代码 / 名称 / 板块 / 行业为公开事实，可直接使用；
--   * 上市与退市日期按公开资料整理，若与交易所披露有出入，一律以交易所公告为准；
--   * 本文件**不含任何行情价格与财务数值** —— 那些必须由 quant-data MCP 从上游
--     （akshare / baostock）真实拉取后落 bars_daily / fundamentals，禁止手写编造；
--   * trading_calendar 仅按「周末休市」规则生成，**不含法定节假日调休**，
--     仅用于离线自测；生产环境请用 data-ops 技能调 quant-data_get_trading_calendar 回写。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1) 标的台账（股票 / ETF / 指数；含一只已退市标的用于幸存者偏差自检）
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO instruments
  (symbol, code, exchange, name, asset_type, board, industry_sw1, list_date, delist_date, lot_size, is_st, index_code, status) VALUES
  -- 股票
  ('600519.SH','600519','SH','贵州茅台','stock','main','食品饮料','2001-08-27',NULL,100,0,'000300',   'active'),
  ('601398.SH','601398','SH','工商银行','stock','main','银行',    '2006-10-27',NULL,100,0,'000300',   'active'),
  ('688981.SH','688981','SH','中芯国际','stock','star','电子',    '2020-07-16',NULL,200,0,'000688',   'active'),
  ('000001.SZ','000001','SZ','平安银行','stock','main','银行',    '1991-04-03',NULL,100,0,'399001',   'active'),
  ('000651.SZ','000651','SZ','格力电器','stock','main','家用电器','1996-11-18',NULL,100,0,'000300',   'active'),
  ('300750.SZ','300750','SZ','宁德时代','stock','chinext','电力设备','2018-06-11',NULL,100,0,'399006','active'),
  -- 已退市标的（刻意保留在库：universe 若只用 payload 里「今天还在交易」的代码，就会系统性高估收益）
  ('600087.SH','600087','SH','长航油运(已退市)','stock','main','交通运输','1997-06-18','2014-06-05',100,0,NULL,'delisted'),
  -- 宽基与行业 ETF（A股账户可直接买卖，零门槛）
  ('510300.SH','510300','SH','沪深300ETF','etf','etf',NULL,'2012-05-28',NULL,100,0,'000300','active'),
  ('510500.SH','510500','SH','中证500ETF','etf','etf',NULL,'2013-03-15',NULL,100,0,'000905','active'),
  ('159915.SZ','159915','SZ','创业板ETF','etf','etf',NULL,'2011-12-09',NULL,100,0,'399006','active'),
  ('512880.SH','512880','SH','证券ETF','etf','etf',NULL,'2016-08-08',NULL,100,0,'399975','active'),
  ('518880.SH','518880','SH','黄金ETF','etf','etf',NULL,'2013-07-29',NULL,100,0,'AU9999','active'),
  -- 指数（不可直接交易，用于基准与成分股选取）
  ('000300.SH','000300','SH','沪深300','index','index',NULL,'2005-04-08',NULL,0,0,NULL,'active'),
  ('000905.SH','000905','SH','中证500','index','index',NULL,'2007-01-15',NULL,0,0,NULL,'active'),
  ('399006.SZ','399006','SZ','创业板指','index','index',NULL,'2010-06-01',NULL,0,0,NULL,'active');

-- ---------------------------------------------------------------------------
-- 2) 交易日历（2026 全年，按周末休市规则生成；法定节假日调休未含，见顶部声明）
-- ---------------------------------------------------------------------------
WITH RECURSIVE d(x) AS (
  SELECT '2026-01-01'
  UNION ALL
  SELECT date(x, '+1 day') FROM d WHERE x < '2026-12-31'
)
INSERT OR IGNORE INTO trading_calendar (cal_date, exchange, is_open, note)
SELECT x, 'SSE',
       CASE WHEN strftime('%w', x) IN ('0','6') THEN 0 ELSE 1 END,
       CASE WHEN strftime('%w', x) IN ('0','6') THEN '周末' ELSE '种子数据：未含法定节假日调休' END
FROM d;

UPDATE trading_calendar
SET prev_open = (
  SELECT MAX(t2.cal_date) FROM trading_calendar t2
  WHERE t2.is_open = 1 AND t2.cal_date < trading_calendar.cal_date
);

-- ---------------------------------------------------------------------------
-- 3) 因子定义（公式与方向是通用量化常识，不含任何回测结论）
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO factor_definitions
  (factor_name, display_name, formula, direction, params_json, lookback_days, category) VALUES
  ('momentum_20d','20日动量','close[t]/close[t-20] - 1','higher_better','{"window":20}',20,'momentum'),
  ('momentum_60d','60日动量','close[t]/close[t-60] - 1','higher_better','{"window":60}',60,'momentum'),
  ('reversal_5d','5日反转','-( close[t]/close[t-5] - 1 )','higher_better','{"window":5}',5,'momentum'),
  ('vol_20d','20日波动率','std(日收益率, 20)','lower_better','{"window":20}',21,'volatility'),
  ('ma_bias_20d','20日乖离率','close[t]/MA(close,20) - 1','neutral','{"window":20}',20,'momentum'),
  ('turnover_20d','20日均换手率','mean(turnover_rate, 20)','neutral','{"window":20}',20,'liquidity'),
  ('pe_ttm','市盈率TTM','mkt_cap / 归母净利润TTM（须用 pub_date <= 调仓日的最近一期）','lower_better','{}',0,'value'),
  ('pb','市净率','mkt_cap / 归母净资产','lower_better','{}',0,'value'),
  ('roe_ttm','净资产收益率TTM','归母净利润TTM / 归母净资产（须用 pub_date <= 调仓日的最近一期）','higher_better','{}',0,'quality'),
  ('profit_yoy','净利润同比','(本期归母净利 - 去年同期) / |去年同期|（须用 pub_date <= 调仓日的最新公告）','higher_better','{}',0,'growth');

-- ---------------------------------------------------------------------------
-- 4) 策略定义（本项目自带的参数，非回测结论）
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO strategies (strategy_id, name, description, universe_json, params_json, risk_limits_json, rebalance_rule) VALUES
  ('etf-momentum-20d','宽基ETF动量轮动',
   '在 510300/510500/159915/518880 四只 ETF 中按 20 日动量排序，持有动量最高的 2 只（等权）。',
   '{"asset_types":["etf"],"symbols":["510300.SH","510500.SH","159915.SZ","518880.SH"],"exclude_st":true}',
   '{"factor":"momentum_20d","top_n":2,"weighting":"equal","ma_filter":60}',
   '{"max_position_pct":0.4,"max_order_amount":100000,"stop_loss":0.08,"min_cash_pct":0.05,"allow_leverage":false}',
   '每月首个交易日'),
  ('hs300-value-quality','沪深300 价值质量精选',
   '在沪深300成分股中按 pe_ttm 与 roe_ttm 双因子打分，取前 20 只、等权，季度调仓。',
   '{"asset_types":["stock"],"index_code":"000300","exclude_st":true,"exclude_delisted":false}',
   '{"factors":["pe_ttm","roe_ttm"],"composite":"z_score_mean","top_n":20,"weighting":"equal"}',
   '{"max_position_pct":0.05,"max_order_amount":50000,"stop_loss":0.15,"min_cash_pct":0.05,"allow_leverage":false}',
   '每季首个交易日');

-- ---------------------------------------------------------------------------
-- 5) 其余表（bars_daily / fundamentals / factor_values / backtests / signals /
--    risk_reviews / order_intents / orders / trades / positions_daily）
--    刻意留空：必须由 quant-data 取数、回测与风控流程真实写入，
--    任何预置的「漂亮数字」都会污染风险复核的证据链。
-- ---------------------------------------------------------------------------