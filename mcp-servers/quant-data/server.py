#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""quant-data —— quant-agent 的行情 / 标的 / 基本面取数 MCP server。

设计原则
--------
1. **只读语义 + 本地缓存**：对外只提供查询工具；唯一写入是把上游拉到的行情/基本面
   落进 quant-db 的 ``bars_daily`` / ``fundamentals``，避免重复拉取与上游限频。
2. **双源**：主源 akshare（免登录、接口全），复权因子与日线补位源 baostock。
   两源口径差异用于交叉校验（见 docs/architecture.md「回测可信度」）。
3. **绝不挂死**：a 股数据源在盘中/收盘后常出现接口抖动，所有上游调用都带
   超时 + 有限重试，失败返回结构化错误信封（``{"ok": false, "error": {...}}``），
   而不是抛异常或无限等待。
4. **防前视**：``get_fundamentals`` 必须带 ``pub_date``（公告日），并强制
   ``pub_date <= as_of_date``；查不到公告日的记录直接丢弃并计入 warnings。

环境变量
--------
``QUANT_DB_PATH``      quant-db 的 SQLite 绝对路径；留空则纯线上查询、不落缓存。
``QUANT_DATA_TIMEOUT`` 单次上游调用超时秒数，默认 25。
``QUANT_DATA_RETRY``   上游调用重试次数，默认 2（合计最多 3 次尝试）。

Windows 中文系统注意：本进程必须运行在 UTF-8 模式下（``PYTHONUTF8=1`` +
``PYTHONIOENCODING=utf-8``），否则向 stdout 写中文会被按 GBK 编码，
MCP 客户端解码失败，表现为工具调用 60 秒超时。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize as nz  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

DB_PATH = os.environ.get("QUANT_DB_PATH", "").strip()
UPSTREAM_TIMEOUT = float(os.environ.get("QUANT_DATA_TIMEOUT", "25"))
UPSTREAM_RETRY = int(os.environ.get("QUANT_DATA_RETRY", "2"))
CACHE_STALE_DAYS = int(os.environ.get("QUANT_CACHE_STALE_DAYS", "1"))

mcp = FastMCP("quant-data")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="quant-data")


# ===========================================================================
# 基础设施：JSON 输出 / 超时重试 / SQLite 缓存
# ===========================================================================
def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _call_with_guard(fn: Callable, *args, timeout: float = None, **kwargs):
    """在线程里执行上游调用，带超时与重试。超时返回 (None, 'timeout')。"""
    timeout = timeout or UPSTREAM_TIMEOUT
    last_error = ""
    for attempt in range(UPSTREAM_RETRY + 1):
        future = _pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout), ""
        except FutureTimeout:
            last_error = f"{getattr(fn, '__name__', fn)} 超时（>{timeout:.0f}s）"
        except Exception as exc:  # 上游抛什么都吞掉，统一转结构化错误
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < UPSTREAM_RETRY:
            time.sleep(0.6 * (attempt + 1))
    return None, last_error


def _db() -> Optional[sqlite3.Connection]:
    if not DB_PATH:
        return None
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _cache_bars(rows: Iterable[dict]) -> tuple[int, str]:
    """把归一化后的日线写入 bars_daily；返回 (写入行数, 警告)。"""
    rows = [r for r in rows if r]
    if not rows:
        return 0, ""
    con = _db()
    if con is None:
        return 0, "未配置 QUANT_DB_PATH 或无写权限，本次结果未落缓存（不影响返回值）"
    try:
        if not _table_exists(con, "bars_daily"):
            return 0, "quant-db 中缺少 bars_daily 表，未落缓存（请先运行 scripts/install.ps1）"
        con.executemany(
            """INSERT INTO bars_daily
                 (symbol, trade_date, open, high, low, close, pre_close, volume,
                  amount, turnover_rate, adj_factor, adjust_mode, source, updated_at)
               VALUES (:symbol,:trade_date,:open,:high,:low,:close,:pre_close,:volume,
                       :amount,:turnover_rate,:adj_factor,:adjust_mode,:source,
                       datetime('now','localtime'))
               ON CONFLICT(symbol, trade_date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, pre_close=excluded.pre_close,
                 volume=excluded.volume, amount=excluded.amount,
                 turnover_rate=excluded.turnover_rate, adj_factor=excluded.adj_factor,
                 adjust_mode=excluded.adjust_mode, source=excluded.source,
                 updated_at=excluded.updated_at""",
            rows,
        )
        con.commit()
        return len(rows), ""
    except sqlite3.Error as exc:
        return 0, f"落缓存失败（{exc}）；返回值仍然有效"
    finally:
        con.close()


def _cache_instruments(rows: Iterable[dict]) -> tuple[int, str]:
    rows = [r for r in rows if r]
    if not rows:
        return 0, ""
    con = _db()
    if con is None:
        return 0, ""
    try:
        if not _table_exists(con, "instruments"):
            return 0, ""
        con.executemany(
            """INSERT INTO instruments
                 (symbol, code, exchange, name, asset_type, board, list_date,
                  lot_size, is_st, status, updated_at)
               VALUES (:symbol,:code,:exchange,:name,:asset_type,:board,:list_date,
                       :lot_size,:is_st,:status, datetime('now','localtime'))
               ON CONFLICT(symbol) DO UPDATE SET
                 name=excluded.name, asset_type=excluded.asset_type,
                 board=excluded.board, lot_size=excluded.lot_size,
                 is_st=excluded.is_st, status=excluded.status,
                 updated_at=excluded.updated_at""",
            rows,
        )
        con.commit()
        return len(rows), ""
    except sqlite3.Error as exc:
        return 0, f"标的台账落库失败（{exc}）"
    finally:
        con.close()


def _load_module(name: str):
    """惰性导入可选依赖，缺失时返回 (None, 错误说明)。"""
    try:
        return __import__(name), ""
    except Exception as exc:  # ImportError / 依赖链报错都算
        return None, f"未安装 {name}（{type(exc).__name__}: {exc}）"


# ===========================================================================
# 交易日历
# ===========================================================================
@mcp.tool()
def get_trading_calendar(start_date: str, end_date: str,
                         exchange: str = "SSE") -> str:
    """获取 A 股交易日历。

    Args:
        start_date: 起始日 YYYY-MM-DD（含）。
        end_date: 结束日 YYYY-MM-DD（含）。
        exchange: SSE / SZSE（两市日历基本一致，保留参数以便未来扩展）。

    Returns:
        JSON 字符串。data 为 [{cal_date, is_open, prev_open, note}, ...]。
        上游不可用时回退到 quant-db 的 trading_calendar 表，并给出 source 字段。
    """
    tool = "get_trading_calendar"
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "日期必须为 YYYY-MM-DD",
                            hint="如需最近 30 个交易日，先取当天再回推"))
    if start_date > end_date:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "start_date 不能晚于 end_date"))

    warnings: list[str] = []
    ak, ak_err = _load_module("akshare")
    rows: list[dict] = []
    if ak is not None:
        def _fetch():
            return ak.tool_trade_date_hist_sina()

        df, fetch_err = _call_with_guard(_fetch)
        if df is not None and len(df) > 0:
            col = "trade_date" if "trade_date" in df.columns else df.columns[0]
            all_days = sorted(str(v)[:10] for v in df[col].tolist())
            open_set = set(all_days)
            cur = datetime.strptime(start_date, "%Y-%m-%d").date()
            stop = datetime.strptime(end_date, "%Y-%m-%d").date()
            while cur <= stop:
                iso = cur.isoformat()
                rows.append({"cal_date": iso, "is_open": 1 if iso in open_set else 0,
                             "prev_open": None, "note": ""})
                cur += timedelta(days=1)
            prev = [d for d in all_days if d < start_date]
            carry = prev[-1] if prev else None
            for row in rows:
                if row["is_open"]:
                    row["prev_open"] = carry
                    carry = row["cal_date"]
        else:
            warnings.append(f"akshare 取交易日历失败，改用本地 trading_calendar：{fetch_err}")
    else:
        warnings.append(f"{ak_err}，改用本地 trading_calendar")

    if not rows:
        con = _db()
        if con is not None and _table_exists(con, "trading_calendar"):
            try:
                cur = con.execute(
                    """SELECT cal_date, is_open, prev_open, note FROM trading_calendar
                       WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date""",
                    (start_date, end_date),
                )
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                con.close()
        if not rows:
            return _json(nz.err(tool, nz.E_EMPTY_DB,
                                "上游与本地交易日均不可用",
                                hint="请确认已运行 scripts/install.ps1 建库，且本机可访问 akshare 上游"))

    return _json(nz.ok(tool, rows, warnings=warnings,
                       open_days=sum(1 for r in rows if r["is_open"])))


# ===========================================================================
# 标的台账
# ===========================================================================
@mcp.tool()
def get_instruments(keyword: str = "", asset_type: str = "",
                    include_delisted: bool = True, limit: int = 200,
                    refresh: bool = False) -> str:
    """查询标的台账（股票 / ETF / 指数），包含退市标的。

    Args:
        keyword: 按代码或名称模糊匹配，如「茅台」「510300」。留空返回全量前 limit 条。
        asset_type: 过滤 stock / etf / index / fund / bond，留空不过滤。
        include_delisted: 是否包含已退市标的（默认 True —— 做回测 universe 时
            **必须**带上它们，否则会犯幸存者偏差）。None 由 True 语义覆盖。
        limit: 最多返回条数，1~2000。
        refresh: 为 True 时先从上游刷新台账再返回（较慢，首次安装后用一次即可）。

    Returns:
        JSON 字符串，data 为标的数组；同时给出 total / source / warnings。
    """
    tool = "get_instruments"
    limit = max(1, min(int(limit or 200), 2000))
    warnings: list[str] = []

    if refresh:
        ak, ak_err = _load_module("akshare")
        if ak is None:
            warnings.append(f"{ak_err}，refresh 被跳过，改读本地台账")
        else:
            fresh: list[dict] = []
            df, name_err = _call_with_guard(ak.stock_info_a_code_name)
            if df is not None and len(df) > 0:
                for code, name in zip(df.iloc[:, 0], df.iloc[:, 1]):
                    code = str(code).zfill(6)
                    try:
                        sym = nz.normalize_symbol(code)
                    except ValueError:
                        continue
                    fresh.append({
                        "symbol": sym, "code": code, "exchange": sym.split(".")[1],
                        "name": str(name), "asset_type": "stock",
                        "board": nz.infer_board(sym, "stock"), "list_date": None,
                        "lot_size": 200 if code.startswith("688") else 100,
                        "is_st": 1 if "ST" in str(name).upper() else 0, "status": "active",
                    })
            else:
                warnings.append(f"上游股票名录获取失败：{name_err}")

            df_etf, etf_err = _call_with_guard(ak.fund_etf_spot_em)
            if df_etf is not None and len(df_etf) > 0:
                code_col = "代码" if "代码" in df_etf.columns else df_etf.columns[0]
                name_col = "名称" if "名称" in df_etf.columns else df_etf.columns[1]
                for code, name in zip(df_etf[code_col], df_etf[name_col]):
                    code = str(code).zfill(6)
                    try:
                        sym = nz.normalize_symbol(code)
                    except ValueError:
                        continue
                    fresh.append({
                        "symbol": sym, "code": code, "exchange": sym.split(".")[1],
                        "name": str(name), "asset_type": "etf", "board": "etf",
                        "list_date": None, "lot_size": 100, "is_st": 0, "status": "active",
                    })
            else:
                warnings.append(f"上游 ETF 名录获取失败：{etf_err}")

            written, cache_warn = _cache_instruments(fresh)
            if cache_warn:
                warnings.append(cache_warn)
            else:
                warnings.append(f"已刷新标的台账 {written} 条（上游不含退市标的，本地退市记录被保留）")

    con = _db()
    rows: list[dict] = []
    source = "db"
    if con is not None and _table_exists(con, "instruments"):
        sql = "SELECT symbol, code, exchange, name, asset_type, board, industry_sw1, list_date, delist_date, lot_size, is_st, index_code, status FROM instruments WHERE 1=1"
        args: list[Any] = []
        if keyword:
            sql += " AND (symbol LIKE ? OR code LIKE ? OR name LIKE ?)"
            args += [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]
        if asset_type:
            sql += " AND asset_type = ?"
            args.append(asset_type)
        if not include_delisted:
            sql += " AND delist_date IS NULL"
        sql += " ORDER BY asset_type, symbol LIMIT ?"
        args.append(limit)
        try:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.Error as exc:
            warnings.append(f"读本地台账失败：{exc}")
        finally:
            con.close()
    else:
        source = "akshare-live"
        warnings.append("quant-db 未就绪，本工具仅返回上游实时名录（不含退市标的与上市日期）")
        if not refresh:
            return _json(nz.err(tool, nz.E_EMPTY_DB,
                                "本地标的台账不可用",
                                hint="运行 scripts/install.ps1 建库并灌入 seed.sql，或本工具传 refresh=True"))

    return _json(nz.ok(tool, rows, source=source, total=len(rows),
                       includes_delisted=any(r.get("delist_date") for r in rows),
                       note="delist_date 非空表示已退市，做 universe 时务必保留以规避幸存者偏差",
                       warnings=warnings))


# ===========================================================================
# 日线行情
# ===========================================================================
def _fetch_bars_akshare(ak, symbol: str, start: str, end: str) -> tuple[list[dict], str]:
    """akshare 不复权 OHLCV；ETF 与股票走不同接口。"""
    code, _ = nz.split_symbol(symbol)
    s, e = start.replace("-", ""), end.replace("-", "")
    is_etf = code.startswith(("51", "56", "58", "50", "15", "16", "18", "11", "12"))
    if is_etf:
        df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=s,
                                 end_date=e, adjust="")
    else:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=s,
                                end_date=e, adjust="")
    if df is None or len(df) == 0:
        return [], "上游返回空数据"
    rows = [nz.normalize_bar_row(rec, symbol, source="akshare")
            for rec in df.to_dict("records")]
    return rows, ""


def _fetch_adj_factors(bs, symbol: str, start: str, end: str) -> tuple[dict, str]:
    """baostock 累计复权因子，作为 qfq/hfq 换算基准。"""
    code, ex = nz.split_symbol(symbol)
    bs_code = f"{ex.lower()}.{code}"
    rs = bs.query_adjust_factor(code=bs_code, start_date=start, end_date=end)
    if rs.error_code != "0":
        return {}, f"baostock 复权因子查询失败：{rs.error_msg}"
    out: dict[str, float] = {}
    while rs.next():
        row = rs.get_row_data()
        # 字段：code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor
        out[str(row[1])] = float(row[4]) if row[4] else 1.0
    return out, ""


@mcp.tool()
def get_daily_bars(symbols: str, start_date: str, end_date: str,
                   adjust: str = "qfq", use_cache: bool = True) -> str:
    """获取日线行情（带复权因子，明确复权口径）。

    Args:
        symbols: 逗号分隔的标的，如 "510300,600519.SH"。最多 20 个。
        start_date: YYYY-MM-DD（含）。
        end_date: YYYY-MM-DD（含）。
        adjust: 价格口径 —— none（不复权）/ qfq（前复权）/ hfq（后复权），默认 qfq。
            **量（volume/amount）永远是原始未复权口径**，禁止与复权价交叉使用。
        use_cache: 优先读 quant-db 的 bars_daily，缺的日期段再补拉上游（默认 True）。

    Returns:
        JSON 字符串。data 为 {symbol: [bar, ...]}，bar 含 date/open/high/low/close/
        volume/amount/turnover_rate/adj_factor/adjust_mode/source。
        上游失败或部分标的取不到时，在该 symbol 下给 ``error`` 字段并计入 warnings，
        其余标的照常返回（不整单失败）。
    """
    tool = "get_daily_bars"
    try:
        sym_list = [nz.normalize_symbol(s) for s in str(symbols).replace("，", ",").split(",") if s.strip()]
    except ValueError as exc:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, str(exc),
                            hint="示例：symbols=\"510300,600519.SH,159915\""))
    if not sym_list:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "symbols 不能为空"))
    if len(sym_list) > 20:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "单次最多 20 个标的",
                            hint="请分批调用，避免上游限频"))
    if adjust.lower() not in ("none", "qfq", "hfq"):
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT,
                            "adjust 只能取 none / qfq / hfq"))

    warnings: list[str] = []
    ak, ak_err = _load_module("akshare")
    bs, bs_err = None, ""
    if adjust.lower() != "none":
        bs, bs_err = _load_module("baostock")
        if bs is not None:
            lg, lg_err = _call_with_guard(bs.login, timeout=15)
            if lg is None or getattr(lg, "error_code", "0") != "0":
                warnings.append(f"baostock 登录失败，复权因子按 1.0 处理：{lg_err or getattr(lg, 'error_msg', '')}")
                bs = None

    result: dict[str, Any] = {}
    try:
        for symbol in sym_list:
            frames: dict[str, dict] = {}

            if use_cache:
                con = _db()
                if con is not None and _table_exists(con, "bars_daily"):
                    try:
                        cur = con.execute(
                            """SELECT trade_date, open, high, low, close, pre_close, volume,
                                      amount, turnover_rate, adj_factor, adjust_mode, source
                               FROM bars_daily WHERE symbol=? AND trade_date BETWEEN ? AND ?
                               ORDER BY trade_date""",
                            (symbol, start_date, end_date))
                        for r in cur.fetchall():
                            frames[r["trade_date"]] = dict(r)
                    finally:
                        con.close()

            need_upstream = (len(frames) == 0)
            fetched: list[dict] = []
            factors: dict[str, float] = {}

            if need_upstream:
                if ak is None:
                    result[symbol] = {"error": nz.err(tool, nz.E_UPSTREAM_FAILED,
                                                      f"akshare 不可用：{ak_err}")["error"]}
                    continue
                fetched, fetch_err = _call_with_guard(
                    _fetch_bars_akshare, ak, symbol, start_date, end_date,
                    timeout=max(UPSTREAM_TIMEOUT, 45))
                if fetch_err:
                    warnings.append(f"{symbol} 上游取数失败：{fetch_err}")
                if not fetched:
                    result[symbol] = {"error": nz.err(
                        tool, nz.E_UPSTREAM_FAILED,
                        f"{symbol} 未取到 {start_date}~{end_date} 的日线",
                        hint="确认代码正确且区间内含交易日；上游偶发抖动可稍后重试")["error"]}
                    continue

                if adjust.lower() != "none" and bs is not None:
                    factors, fac_err = _call_with_guard(
                        _fetch_adj_factors, bs, symbol, start_date, end_date)
                    if fac_err:
                        warnings.append(f"{symbol} 复权因子不可用（{fac_err}），adj_factor 按 1.0")

            if adjust.lower() != "none" and not factors and bs is not None:
                factors, _ = _call_with_guard(
                    _fetch_adj_factors, bs, symbol, start_date, end_date)

            for row in fetched:
                day = row["trade_date"]
                row["adj_factor"] = factors.get(day, 1.0)
                row["adjust_mode"] = "hfq"        # 库里统一存后复权因子口径
                row["pre_close"] = None
                frames[day] = row

            if fetched:
                written, cache_warn = _cache_bars(fetched)
                if cache_warn:
                    warnings.append(cache_warn)
                elif written:
                    warnings.append(f"{symbol} 已补拉并落缓存 {written} 行")

            days = sorted(frames)
            if not days:
                result[symbol] = {"error": nz.err(
                    tool, nz.E_NOT_FOUND, f"{symbol} 在 {start_date}~{end_date} 无缓存数据")["error"]}
                continue

            latest_factor = frames[days[-1]].get("adj_factor") or 1.0
            bars = []
            for i, day in enumerate(days):
                raw = frames[day]
                bar = {
                    "date": day,
                    "open": nz.convert_price(raw.get("open"), raw.get("adj_factor"), latest_factor, adjust),
                    "high": nz.convert_price(raw.get("high"), raw.get("adj_factor"), latest_factor, adjust),
                    "low": nz.convert_price(raw.get("low"), raw.get("adj_factor"), latest_factor, adjust),
                    "close": nz.convert_price(raw.get("close"), raw.get("adj_factor"), latest_factor, adjust),
                    "volume": raw.get("volume"),
                    "amount": raw.get("amount"),
                    "turnover_rate": raw.get("turnover_rate"),
                    "adj_factor": raw.get("adj_factor"),
                    "adjust_mode": adjust.lower(),
                    "source": raw.get("source") or "cache",
                }
                if i > 0:
                    bar["pre_close"] = nz.convert_price(
                        frames[days[i - 1]].get("close"),
                        frames[days[i - 1]].get("adj_factor"), latest_factor, adjust)
                bars.append(bar)
            result[symbol] = bars
    finally:
        if bs is not None:
            _call_with_guard(bs.logout, timeout=10)

    total = sum(len(v) for k, v in result.items() if isinstance(v, list))
    return _json(nz.ok(tool, result, adjust=adjust.lower(), bars_total=total,
                       warnings=warnings,
                       volume_note="volume/amount 为未复权原始口径，勿与复权价混用"))


# ===========================================================================
# 基本面（pub_date 防前视）
# ===========================================================================
@mcp.tool()
def get_fundamentals(symbols: str, as_of_date: str = "",
                     report_periods: str = "", limit: int = 500) -> str:
    """获取基本面数据，**强制带公告日（pub_date）以支持 as-of 查询**。

    Args:
        symbols: 逗号分隔的标的代码，留空表示不限（配合 report_periods 批量取）。
        as_of_date: 观察日 YYYY-MM-DD。只返回 ``pub_date <= as_of_date`` 的记录 ——
            这是防止前视偏差的关键：回测在 T 日只能看到 T 日前已公告的财报。
            留空则返回库中全部（仅供人工核查，回测禁止这样用）。
        report_periods: 逗号分隔的报告期 YYYY-MM-DD，如 "2024-09-30,2024-06-30"。
            留空时默认取最近 4 个季末，用于从上游补拉。
        limit: 最多返回条数。

    Returns:
        JSON 字符串。data 为基本面数组，含 report_period / pub_date / pe_ttm / pb /
        roe / revenue_yoy / profit_yoy / mkt_cap。
        无公告日的记录会被**丢弃**并计入 warnings —— 宁缺毋滥。
    """
    tool = "get_fundamentals"
    if as_of_date:
        try:
            datetime.strptime(as_of_date, "%Y-%m-%d")
        except ValueError:
            return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "as_of_date 必须为 YYYY-MM-DD"))

    sym_filter: list[str] = []
    if symbols.strip():
        try:
            sym_filter = [nz.normalize_symbol(s) for s in symbols.replace("，", ",").split(",") if s.strip()]
        except ValueError as exc:
            return _json(nz.err(tool, nz.E_BAD_ARGUMENT, str(exc)))

    warnings: list[str] = []
    con = _db()
    rows: list[dict] = []
    if con is not None and _table_exists(con, "fundamentals"):
        sql = ("SELECT symbol, report_period, pub_date, pe_ttm, pb, roe, revenue_yoy,"
               " profit_yoy, mkt_cap, source FROM fundamentals WHERE pub_date IS NOT NULL")
        args: list[Any] = []
        if sym_filter:
            sql += f" AND symbol IN ({','.join('?' * len(sym_filter))})"
            args += sym_filter
        if as_of_date:
            sql += " AND pub_date <= ?"
            args.append(as_of_date)
        sql += " ORDER BY symbol, pub_date DESC LIMIT ?"
        args.append(int(limit))
        try:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.Error as exc:
            warnings.append(f"读本地基本面失败：{exc}")
        finally:
            con.close()

    if not rows:
        warnings.append("本地 fundamentals 无可用记录（需 pub_date 非空），已尝试从上游补拉")
        ak, ak_err = _load_module("akshare")
        if ak is None:
            return _json(nz.err(tool, nz.E_UPSTREAM_FAILED, f"akshare 不可用：{ak_err}",
                                hint="先运行 data-ops 技能在线补数，或检查本机网络"))
        periods = [p.strip() for p in report_periods.replace("，", ",").split(",") if p.strip()]
        if not periods:
            today = date.today()
            q_ends = []
            for year in (today.year, today.year - 1):
                q_ends += [f"{year}-12-31", f"{year}-09-30", f"{year}-06-30", f"{year}-03-31"]
            periods = [p for p in q_ends if p <= today.isoformat()][:4]

        fetched: list[dict] = []
        for period in periods:
            def _fetch(period=period):
                return ak.stock_yjbb_em(date=period.replace("-", ""))

            df, err = _call_with_guard(_fetch, timeout=max(UPSTREAM_TIMEOUT, 45))
            if df is None or len(df) == 0:
                warnings.append(f"报告期 {period} 上游无数据：{err}")
                continue
            for rec in df.to_dict("records"):
                code = str(rec.get("股票代码", "")).zfill(6)
                if not code.isdigit():
                    continue
                try:
                    sym = nz.normalize_symbol(code, nz.infer_exchange(code))
                except ValueError:
                    continue
                if sym_filter and sym not in sym_filter:
                    continue
                pub = str(rec.get("最新公告日期", ""))[:10]
                if not pub or pub == "NaT":
                    continue
                fetched.append({
                    "symbol": sym, "report_period": period, "pub_date": pub,
                    "pe_ttm": None, "pb": None,
                    "roe": _num(rec.get("净资产收益率")),
                    "revenue_yoy": _num(rec.get("营业总收入-同比增长")),
                    "profit_yoy": _num(rec.get("净利润-同比增长")),
                    "mkt_cap": None, "source": "akshare.stock_yjbb_em",
                })
            warnings.append(f"报告期 {period} 补拉 {len(fetched)} 条（累计）")

        if fetched:
            _cache_fundamentals(fetched)
            if as_of_date:
                rows = [r for r in fetched if r["pub_date"] <= as_of_date]
            else:
                rows = fetched
            dropped = len(fetched) - len(rows)
            if dropped:
                warnings.append(f"已按 as_of_date={as_of_date} 剔除 {dropped} 条「尚未公告」的记录（防前视）")

    return _json(nz.ok(tool, rows, as_of_date=as_of_date or None, total=len(rows),
                       warnings=warnings,
                       rule="as_of 查询只暴露 pub_date <= as_of_date 的财报；回测调仓必须带 as_of_date"))


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-", "--"):
            return None
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _cache_fundamentals(rows: list[dict]) -> tuple[int, str]:
    con = _db()
    if con is None:
        return 0, ""
    try:
        if not _table_exists(con, "fundamentals"):
            return 0, ""
        con.executemany(
            """INSERT INTO fundamentals
                 (symbol, report_period, pub_date, pe_ttm, pb, roe, revenue_yoy,
                  profit_yoy, mkt_cap, source, updated_at)
               VALUES (:symbol,:report_period,:pub_date,:pe_ttm,:pb,:roe,:revenue_yoy,
                       :profit_yoy,:mkt_cap,:source, datetime('now','localtime'))
               ON CONFLICT(symbol, report_period) DO UPDATE SET
                 pub_date=excluded.pub_date, roe=excluded.roe,
                 revenue_yoy=excluded.revenue_yoy, profit_yoy=excluded.profit_yoy,
                 updated_at=excluded.updated_at""",
            rows)
        con.commit()
        return len(rows), ""
    except sqlite3.Error as exc:
        return 0, f"基本面落库失败（{exc}）"
    finally:
        con.close()


# ===========================================================================
# 涨跌停 / 停牌状态
# ===========================================================================
@mcp.tool()
def get_limit_status(symbols: str, trade_date: str) -> str:
    """查询指定交易日的涨跌停价、是否停牌、是否一字板。

    Args:
        symbols: 逗号分隔标的代码。
        trade_date: 交易日 YYYY-MM-DD。

    Returns:
        JSON 字符串。data 为 [{symbol, trade_date, pre_close, limit_up, limit_down,
        is_suspended, is_one_word_board, board, limit_pct, tradable}]。
        取不到前收（当日无行情且库里无缓存）时 tradable=false 并给 error 字段 ——
        **不可判定即视为不可交易**，这是保守方向的降级。
    """
    tool = "get_limit_status"
    try:
        sym_list = [nz.normalize_symbol(s) for s in symbols.replace("，", ",").split(",") if s.strip()]
    except ValueError as exc:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, str(exc)))
    if not sym_list:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "symbols 不能为空"))
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "trade_date 必须为 YYYY-MM-DD"))

    con = _db()
    if con is None:
        return _json(nz.err(tool, nz.E_EMPTY_DB, "quant-db 不可用，无法计算涨跌停价",
                            hint="涨跌停价依赖 bars_daily 的前收与 instruments 的板块信息，请先安装并取数"))
    out: list[dict] = []
    warnings: list[str] = []
    try:
        if not (_table_exists(con, "bars_daily") and _table_exists(con, "instruments")):
            return _json(nz.err(tool, nz.E_EMPTY_DB, "缺少 bars_daily / instruments 表",
                                hint="请先运行 scripts/install.ps1"))
        for symbol in sym_list:
            meta = con.execute(
                "SELECT board, is_st, status, delist_date FROM instruments WHERE symbol=?",
                (symbol,)).fetchone()
            board = (meta["board"] if meta else None) or nz.infer_board(symbol)
            is_st = bool(meta["is_st"]) if meta else False
            delisted = bool(meta["delist_date"]) if meta else False
            pct = nz.price_limit_pct(board, is_st)

            bar = con.execute(
                """SELECT trade_date, open, high, low, close, pre_close, volume
                   FROM bars_daily WHERE symbol=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 2""",
                (symbol, trade_date)).fetchall()
            today = next((dict(b) for b in bar if b["trade_date"] == trade_date), None)
            prev = next((dict(b) for b in bar if b["trade_date"] < trade_date), None)
            if today and not today.get("pre_close") and prev:
                today["pre_close"] = prev.get("close")

            row: dict = {"symbol": symbol, "trade_date": trade_date, "board": board,
                         "limit_pct": pct, "is_st": is_st}
            if delisted:
                row.update(pre_close=None, limit_up=None, limit_down=None,
                           is_suspended=True, is_one_word_board=False, tradable=False,
                           note="该标的已退市（instruments.delist_date 非空）")
                out.append(row)
                continue

            pre_close = (today or {}).get("pre_close")
            if pre_close:
                up, down = nz.limit_prices(pre_close, pct)
                row.update(pre_close=pre_close, limit_up=up, limit_down=down)
            else:
                row.update(pre_close=None, limit_up=None, limit_down=None,
                           error=nz.err(tool, nz.E_NOT_FOUND,
                                        f"{symbol} 缺 {trade_date} 或之前的前收价")["error"])

            suspended = today is None or not today.get("volume")
            row["is_suspended"] = bool(suspended)
            if today and row.get("limit_up"):
                row["is_one_word_board"] = bool(
                    today.get("low") == today.get("high") == row["limit_up"]) or bool(
                    today.get("low") == today.get("high") == row["limit_down"])
            else:
                row["is_one_word_board"] = False
            row["tradable"] = bool(not suspended and row.get("limit_up"))
            out.append(row)
    finally:
        con.close()

    if any(not r.get("tradable") for r in out):
        warnings.append("存在 tradable=false 的标的（停牌/退市/前收缺失）：下单前必须剔除或顺延")
    return _json(nz.ok(tool, out, trade_date=trade_date, warnings=warnings,
                       rule="保守原则：不可判定即不可交易；一字板无成交量时按不可成交处理"))


# ===========================================================================
# 指数成分股
# ===========================================================================
@mcp.tool()
def get_index_members(index_code: str, trade_date: str = "") -> str:
    """获取指数成分股（用于构建 universe）。

    Args:
        index_code: 指数代码，如 000300 / 000905 / 399006 / 000688。
        trade_date: 指定成分生效日（YYYY-MM-DD）。**注意**：多数免费源只提供
            「当前成分」，历史成分需付费源，本工具会如实标注
            ``point_in_time=false``，回测中据此判定是否会引入成分股前视。

    Returns:
        JSON 字符串。data 为 [{symbol, code, name, index_code}]。
    """
    tool = "get_index_members"
    code = str(index_code).strip().split(".")[0]
    if not code.isdigit() or len(code) != 6:
        return _json(nz.err(tool, nz.E_BAD_ARGUMENT, "index_code 应为 6 位数字，如 000300"))

    ak, ak_err = _load_module("akshare")
    if ak is None:
        return _json(nz.err(tool, nz.E_UPSTREAM_FAILED, f"akshare 不可用：{ak_err}"))

    warnings = ["免费数据源多只提供当前成分股；无法保证 point-in-time，历史成分股请使用付费源或用当期成分近似并声明"]
    df, err = _call_with_guard(ak.index_stock_cons_csindex, symbol=code,
                               timeout=max(UPSTREAM_TIMEOUT, 40))
    if df is None or len(df) == 0:
        df, err = _call_with_guard(ak.index_stock_cons, symbol=code,
                                   timeout=max(UPSTREAM_TIMEOUT, 40))
    if df is None or len(df) == 0:
        return _json(nz.err(tool, nz.E_UPSTREAM_FAILED,
                            f"{code} 成分股取不到：{err}",
                            hint="先确认指数代码存在；也可改用 data-ops 技能从本地 instruments.index_code 近似构建"))

    code_col = next((c for c in ("成分券代码", "品种代码", "代码", "证券代码") if c in df.columns), None)
    name_col = next((c for c in ("成分券名称", "品种名称", "名称", "证券简称") if c in df.columns), None)
    if code_col is None:
        return _json(nz.err(tool, nz.E_UPSTREAM_FAILED,
                            f"上游返回列不认识：{list(df.columns)[:8]}"))
    rows = []
    for rec in df.to_dict("records"):
        raw_code = str(rec.get(code_col, "")).zfill(6)
        if not raw_code.isdigit():
            continue
        try:
            sym = nz.normalize_symbol(raw_code, nz.infer_exchange(raw_code))
        except ValueError:
            continue
        rows.append({"symbol": sym, "code": raw_code,
                     "name": str(rec.get(name_col, "")) if name_col else "",
                     "index_code": code})
    return _json(nz.ok(tool, rows, index_code=code, trade_date=trade_date or None,
                       point_in_time=False, total=len(rows), warnings=warnings))


def _warm_optional_deps() -> None:
    """在主线程预导入重型可选依赖。

    实测：在 MCP 的工作线程里首次 ``import akshare`` 会阻塞 76~100 秒
    （``numpy._core._multiarray_umath`` 在该上下文下初始化异常缓慢），
    远超工具调用超时；预热进 ``sys.modules`` 后 ``_load_module`` 直接命中缓存。
    预热失败不致命，仍由 ``_load_module`` 在调用时兜底。
    """
    for _name in ("akshare", "baostock"):
        try:
            __import__(_name)
        except Exception:
            pass


if __name__ == "__main__":
    _warm_optional_deps()
    mcp.run()
