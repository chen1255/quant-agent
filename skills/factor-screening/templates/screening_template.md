# 因子筛选单（模板）

> 复用本结构生成筛选结果。带 `{}` 的为占位说明，正式输出时替换；不适用的小节保留标题并注明"无"。

## 一、Universe 与因子

| 项 | 内容 |
|---|---|
| universe | {asset_types / index_code / 显式标的列表} |
| 是否含退市标的 | {是（必须）/ 声明 point_in_time={false}} |
| 因子 | {factor_name（direction, params_json, lookback_days）} |
| as_of 日期 | {yyyy-MM-dd} |
| 取前 N / 权重 | {N / equal / …} |

## 二、计算口径

- 价格口径：{qfq / hfq}；量恒为未复权原始口径。
- 财务口径：仅 `pub_date <= as_of` 的最新一期；无公告日记录已丢弃 {N} 条。
- 标准化：同一 trade_date 内 z-score + pct_rank；多因子方向已统一。

## 三、因子值与排名

| 排名 | 代码 | 名称 | {factor 原值} | z_score | pct_rank | 合成分 | 入池 |
|---|---|---|---|---|---|---|---|
| 1 | {510300.SH} | {沪深300ETF} | {…} | {…} | {…} | {…} | {是/否} |
| 2 |  |  |  |  |  |  |  |

> 样本不足（< lookback_days）或数据缺失被剔除的标的：{逐条列出，或写"无"}

## 四、候选池

| 代码 | 名称 | 方向 | target_weight | 备注 |
|---|---|---|---|---|
| {510300.SH} | {沪深300ETF} | {buy} | {0.5} | {…} |

## 五、落库记录

| 表 | 主键/关键字段 | 写入行数 | 状态 |
|---|---|---|---|
| factor_values | {factor_name, symbol, trade_date} | {N} | {成功/失败} |
| signals | {signal_id, trade_date, symbol, side} | {N} | {成功/失败/未生成} |

> 信号 `trade_date` 为生成日，成交在下一交易日（T+1）。

## 六、局限声明

- 指数成分：{point_in_time=false，历史成分无法保证}
- 数据 warnings：{逐条；无则写"无"}
- 声明：本单只产出候选池与因子值，未评估策略历史收益、未产出执行计划。
