#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trade-gateway —— quant-agent 的实盘交易 MCP server（**默认关闭**）。

安全设计（四道闸，代码层强制，不是提示词层自觉）
------------------------------------------------
1. **默认关闭**：本 server 在 ``extensions_config.json`` 中 ``enabled:false``；
   只有 ``install.ps1 -EnableTrading`` 才写 true。且 ``QUANT_TRADE_DRY_RUN`` 默认 1，
   即「打开」时也只写本地 ``orders`` 表、不报券商。
2. **风控凭证硬闸门**：``prepare_order`` 必须带 ``risk_review_id``，且该记录
   ``verdict='通过'``。少了或结论不对，直接在 MCP 层拒绝，不生成确认码。
3. **人工确认码**：``prepare_order`` 返回 6 位 ``confirm_token``（2 分钟 TTL、单次消费）。
   代码由用户从对话里贴回，模型不得编造。
4. **下单前重校验**：``place_order`` 复核 token 时效与消费状态、风控 id、单笔与仓位上限、
   最小交易单位、涨跌停、停牌；``dry_run=1`` 时只写 orders 表。

依赖 xtquant 时才会连券商；未安装 xtquant / 未运行 QMT 时，账户类工具返回结构化
"unavailable" 而不是崩溃，``prepare_order`` / ``place_order`` 的 dry_run 通路仍然可用
（用于离线演练与回归测试）。

环境变量
--------
``QUANT_DB_PATH``         quant-db 的 SQLite 绝对路径（必需：意图/订单/成交都落这里）。
``QUANT_TRADE_DRY_RUN``   1=只写库不报单（默认），0=真实报单。
``QUANT_QMT_PATH``        QMT 客户端安装目录（qmt_path），如 ``C:\\国金QMT\\userdata_mini``。
``QUANT_QMT_ACCOUNT``     资金账号。
``QUANT_QMT_ACCOUNT_TYPE`` stock=普通股票账户（默认）/ credit=信用账户。
``QUANT_MAX_ORDER_AMOUNT`` 单笔金额上限，默认 100000。
``QUANT_MAX_POSITION_PCT`` 单标的最大仓位占比，默认 0.30。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "quant-data"))   # 复用 quant-data 的代码口径实现

import gates  # noqa: E402
import normalize as nz  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

DB_PATH = os.environ.get("QUANT_DB_PATH", "").strip()
DRY_RUN = os.environ.get("QUANT_TRADE_DRY_RUN", "1").strip() not in ("0", "false", "False")
QMT_PATH = os.environ.get("QUANT_QMT_PATH", "").strip()
QMT_ACCOUNT = os.environ.get("QUANT_QMT_ACCOUNT", "").strip()
QMT_ACCOUNT_TYPE = os.environ.get("QUANT_QMT_ACCOUNT_TYPE", "stock").strip() or "stock"
MAX_ORDER_AMOUNT = float(os.environ.get("QUANT_MAX_ORDER_AMOUNT", "100000"))
MAX_POSITION_PCT = float(os.environ.get("QUANT_MAX_POSITION_PCT", "0.30"))
CANCEL_SELL_ONLY = os.environ.get("QUANT_CANCEL_SELL_ONLY", "1") not in ("0", "false")

mcp = FastMCP("trade-gateway")

_trader_state: dict = {"trader": None, "account": None, "error": ""}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(tool: str, data: Any = None, **extra) -> str:
    return _json(nz.ok(tool, data, dry_run=DRY_RUN, **extra))


def _err(tool: str, code: str, message: str, hint: str = "", details: Any = None) -> str:
    return _json(nz.err(tool, code, message, hint, details))


def _reject(tool: str, exc: gates.GateReject) -> str:
    """闸门拒绝：返回结构化错误 + 明确的下一步，绝不静默放行。"""
    return _json({"ok": False, "tool": tool, "blocked_by_gate": True,
                  "error": exc.to_dict()})


def _conn() -> sqlite3.Connection:
    if not DB_PATH:
        raise gates.GateReject("DB_UNAVAILABLE",
                               "未配置 QUANT_DB_PATH，交易网关无法记录意图与订单",
                               "请先运行 scripts/install.ps1 建库")
    if not Path(DB_PATH).exists():
        raise gates.GateReject("DB_UNAVAILABLE", f"quant-db 不存在：{DB_PATH}")
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _q(sql: str, args: tuple = ()) -> list[dict]:
    """只读查询 quant-db（拿标的元信息 / 前收 / 持仓）。失败返回空列表。"""
    try:
        con = _conn()
    except gates.GateReject:
        return []
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _norm(symbol: str) -> str:
    try:
        return nz.normalize_symbol(symbol)
    except ValueError as exc:
        raise gates.GateReject("BAD_ARGUMENT", str(exc))


# ===========================================================================
# xtquant 接入（惰性、容错）
# ===========================================================================
def _ensure_trader():
    """按需初始化 XtQuantTrader。不可用时把原因写进 _trader_state['error']。"""
    if _trader_state["trader"] is not None:
        return _trader_state["trader"]
    if not QMT_PATH:
        _trader_state["error"] = "未配置 QUANT_QMT_PATH（QMT 客户端 userdata_mini 目录）"
        return None
    if not QMT_ACCOUNT:
        _trader_state["error"] = "未配置 QUANT_QMT_ACCOUNT（资金账号）"
        return None
    try:
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount
    except Exception as exc:  # noqa: BLE001
        _trader_state["error"] = (f"无法导入 xtquant（{type(exc).__name__}: {exc}）。"
                                  "xtquant 随 QMT 客户端分发，请用 -QmtPath 指向其 site-packages")
        return None
    try:
        session_id = random.randint(100000, 999999)
        trader = XtQuantTrader(QMT_PATH, session_id)
        trader.start()
        connect_result = trader.connect()
        if connect_result != 0:
            _trader_state["error"] = f"XtQuantTrader.connect() 返回 {connect_result}，请确认 QMT 已登录并开启极简模式"
            return None
        account = StockAccount(QMT_ACCOUNT, QMT_ACCOUNT_TYPE)
        subscribe_result = trader.subscribe(account)
        if subscribe_result != 0:
            _trader_state["error"] = f"账户订阅失败（{subscribe_result}），请核对资金账号与账户类型"
            return None
        _trader_state.update(trader=trader, account=account, error="")
        return trader
    except Exception as exc:  # noqa: BLE001
        _trader_state["error"] = f"初始化 XtQuantTrader 失败：{type(exc).__name__}: {exc}"
        return None


# ===========================================================================
# 只读：模式 / 账户 / 持仓 / 委托 / 成交
# ===========================================================================
@mcp.tool()
def get_trading_mode() -> str:
    """查看当前交易模式与四道闸的状态（**先调这个**，再决定是否继续下单流程）。

    Returns:
        JSON 字符串：mode 为 ``dry_run`` / ``live`` / ``unavailable``；
        gates 给出四道闸的实时状态；warnings 说明为何不可实盘。
    """
    tool = "get_trading_mode"
    trader = _ensure_trader() if not DRY_RUN else None
    warnings: list[str] = []
    if DRY_RUN:
        mode = "dry_run"
        warnings.append("QUANT_TRADE_DRY_RUN=1：下单只写本地 orders 表，不会报给券商")
    elif trader is None:
        mode = "unavailable"
        warnings.append(_trader_state.get("error") or "xtquant 不可用")
    else:
        mode = "live"

    review_count = _q("SELECT COUNT(*) n FROM risk_reviews WHERE verdict='通过'")
    pending = _q("SELECT COUNT(*) n FROM order_intents WHERE status='pending'")
    return _ok(tool, {
        "mode": mode,
        "dry_run": DRY_RUN,
        "qmt_path": QMT_PATH or None,
        "account": QMT_ACCOUNT or None,
        "account_type": QMT_ACCOUNT_TYPE,
        "positions_available": trader is not None,
        "gates": {
            "gate1_default_off": True,
            "gate2_risk_review": "需 risk_reviews.verdict='通过'",
            "gate3_confirm_token": f"6 位数字，{gates.CONFIRM_TOKEN_TTL}s TTL，单次消费",
            "gate4_precheck": f"单笔上限 {MAX_ORDER_AMOUNT:,.0f} 元，单标的上限 {MAX_POSITION_PCT:.0%}",
        },
        "stats": {"passed_reviews": review_count[0]["n"] if review_count else None,
                  "pending_intents": pending[0]["n"] if pending else None},
    }, warnings=warnings,
       note="模式为 unavailable 时仍可用 prepare_order/place_order 走 dry_run 通路演练全流程")


@mcp.tool()
def get_account() -> str:
    """查询资金账户（总资产 / 可用 / 冻结）。实盘不可用时返回 unavailable。"""
    tool = "get_account"
    if DRY_RUN:
        return _ok(tool, {"account_id": QMT_ACCOUNT or "SIM-001",
                          "total_asset": None, "cash": None, "frozen": None,
                          "market_value": None, "source": "dry_run"},
                   warnings=["dry_run 模式不提供真实资金数据；positions_daily 仅供演示"])
    trader = _ensure_trader()
    if trader is None:
        return _err(tool, "TRADER_UNAVAILABLE", _trader_state.get("error", ""),
                    hint="确认 QMT 已启动、已登录、已开极简模式，且 -QmtPath 正确")
    try:
        asset = trader.query_stock_asset(_trader_state["account"])
    except Exception as exc:  # noqa: BLE001
        return _err(tool, "TRADER_UNAVAILABLE", f"查询资金失败：{exc}")
    if asset is None:
        return _err(tool, "TRADER_UNAVAILABLE", "query_stock_asset 返回空",
                    hint="核对资金账号与账户类型（stock/credit）")
    return _ok(tool, {"account_id": getattr(asset, "account_id", None),
                      "total_asset": getattr(asset, "total_asset", None),
                      "cash": getattr(asset, "cash", None),
                      "frozen": getattr(asset, "frozen_cash", None),
                      "market_value": getattr(asset, "market_value", None),
                      "source": "xtquant"})


@mcp.tool()
def get_positions(as_of_date: str = "") -> str:
    """查询持仓。实盘不可用时回退到 quant-db 的 positions_daily 日终快照。

    Args:
        as_of_date: YYYY-MM-DD；非空时读该日快照（用于复现历史决策），留空取最新。
    """
    tool = "get_positions"
    trader = None if DRY_RUN else _ensure_trader()
    if trader is not None:
        try:
            rows = trader.query_stock_positions(_trader_state["account"]) or []
            data = [{"symbol": f"{getattr(p, 'stock_code', '')}",
                     "qty": getattr(p, "volume", None),
                     "avail_qty": getattr(p, "can_use_volume", None),
                     "avg_cost": getattr(p, "open_price", None),
                     "market_value": getattr(p, "market_value", None),
                     "source": "xtquant"} for p in rows]
            return _ok(tool, data, source="xtquant")
        except Exception as exc:  # noqa: BLE001
            return _err(tool, "TRADER_UNAVAILABLE", f"查询持仓失败：{exc}")

    if as_of_date:
        rows = _q("""SELECT * FROM positions_daily WHERE trade_date=?""", (as_of_date,))
    else:
        rows = _q("""SELECT * FROM positions_daily
                     WHERE trade_date=(SELECT MAX(trade_date) FROM positions_daily)""")
    return _ok(tool, rows, source="positions_daily",
               warnings=["xtquant 不可用或处于 dry_run，返回的是本地日终快照，不是实时持仓"])


@mcp.tool()
def get_orders(limit: int = 50, status: str = "") -> str:
    """查询订单（本地 orders 表 + 实盘委托合并视角）。"""
    tool = "get_orders"
    limit = max(1, min(int(limit or 50), 500))
    args: list[Any] = []
    sql = """SELECT order_id, intent_id, symbol, side, order_type, price, qty,
                    filled_qty, avg_fill_price, fee, tax, transfer_fee, status,
                    is_dry_run, xt_order_id, reject_reason, submitted_at
             FROM orders WHERE 1=1"""
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY COALESCE(submitted_at,0) DESC, rowid DESC LIMIT ?"
    args.append(limit)
    return _ok(tool, _q(sql, tuple(args)), source="orders")


@mcp.tool()
def get_trades(order_id: str = "", limit: int = 100) -> str:
    """查询成交明细。"""
    tool = "get_trades"
    limit = max(1, min(int(limit or 100), 500))
    if order_id:
        rows = _q("""SELECT * FROM trades WHERE order_id=? ORDER BY trade_time DESC LIMIT ?""",
                  (order_id, limit))
    else:
        rows = _q("""SELECT * FROM trades ORDER BY COALESCE(trade_time,'') DESC LIMIT ?""",
                  (limit,))
    return _ok(tool, rows, source="trades")


# ===========================================================================
# 写：prepare_order（闸2 + 闸3）
# ===========================================================================
@mcp.tool()
def prepare_order(symbol: str, side: str, qty: int, price: float = 0,
                  order_type: str = "limit", risk_review_id: str = "",
                  strategy_id: str = "", signal_id: str = "",
                  account_id: str = "") -> str:
    """**生成下单意图与 6 位确认码**（不报单）。这是下单的第一步。

    闸门：必须先有 ``risk_review_id``，且对应 ``risk_reviews.verdict='通过'``；
    数量须为最小交易单位整数倍；单笔金额不得超上限；停牌/涨跌停越界直接拒绝。

    Args:
        symbol: 标的代码，如 510300 或 600519.SH。
        side: buy / sell。
        qty: 数量（股/份，须为 lot_size 整数倍；A股与 ETF 为 100）。
        price: 限价单价格（元）。市价单本期未开放。
        risk_review_id: risk-reviewer 出具并已落库的复核记录 id（**必填**）。
        strategy_id / signal_id: 溯源用，建议填。
        account_id: 资金账号，留空取默认。

    Returns:
        JSON 字符串，含 ``intent_id`` 与 ``confirm_token``（6 位数字）。
        **必须把 confirm_token 原文展示给用户，由用户确认后贴回**，模型不得自行调用 place_order。
    """
    tool = "prepare_order"
    try:
        sym = _norm(symbol)
        con = _conn()
    except gates.GateReject as exc:
        return _reject(tool, exc)
    try:
        try:
            review = gates.check_risk_review(con, risk_review_id, "signal", signal_id or None)
        except gates.GateReject as exc:
            return _reject(tool, exc)

        meta = _q("SELECT board, is_st, lot_size, delist_date, status FROM instruments WHERE symbol=?",
                  (sym,))
        lot_size = (meta[0]["lot_size"] if meta else 100) or 100
        board = (meta[0]["board"] if meta else None) or nz.infer_board(sym)
        is_st = bool(meta[0]["is_st"]) if meta else False
        delisted = bool(meta[0]["delist_date"]) if meta else False

        bars = _q("""SELECT trade_date, close, pre_close FROM bars_daily
                     WHERE symbol=? ORDER BY trade_date DESC LIMIT 2""", (sym,))
        pre_close = None
        if bars:
            pre_close = bars[0].get("pre_close") or (bars[1].get("close") if len(bars) > 1 else None)
        limit_up = limit_down = None
        if pre_close:
            pct = nz.price_limit_pct(board, is_st)
            limit_up, limit_down = nz.limit_prices(pre_close, pct)

        try:
            amount = gates.check_limits(
                qty=int(qty), price=float(price or 0), lot_size=int(lot_size),
                max_order_amount=MAX_ORDER_AMOUNT, limit_up=limit_up,
                limit_down=limit_down, is_suspended=delisted,
                side=side, order_type=order_type)
        except gates.GateReject as exc:
            return _reject(tool, exc)

        now = int(time.time())
        try:
            intent = gates.create_intent(
                con, symbol=sym, side=side, qty=int(qty), price=float(price or 0) or None,
                order_type=order_type, risk_review_id=review["review_id"], now=now,
                strategy_id=strategy_id or None, signal_id=signal_id or None,
                account_id=account_id or QMT_ACCOUNT or None, est_amount=amount)
        except gates.GateReject as exc:
            return _reject(tool, exc)

        if signal_id:
            try:
                con.execute("UPDATE signals SET risk_review_id=?, status='approved' "
                            "WHERE signal_id=? AND status IN ('new','reviewed')",
                            (review["review_id"], signal_id))
                con.commit()
            except sqlite3.Error:
                con.rollback()
    finally:
        con.close()

    costs = gates.estimate_costs(side=side, price=float(price or 0), qty=int(qty))
    return _ok(tool, {
        "intent_id": intent["intent_id"],
        "confirm_token": intent["confirm_token"],
        "expires_at": intent["expires_at"],
        "expires_at_readable": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(intent["expires_at"])),
        "ttl_seconds": intent["ttl_seconds"],
        "risk_review": {"review_id": review["review_id"], "verdict": review["verdict"],
                        "reviewer": review.get("reviewer")},
        "order": {"symbol": sym, "side": side, "qty": int(qty),
                  "price": float(price or 0) or None, "order_type": order_type,
                  "est_amount": amount, "lot_size": lot_size,
                  "limit_up": limit_up, "limit_down": limit_down},
        "estimated_costs": costs,
    }, warnings=["确认码 2 分钟内有效、只能使用一次；请把原文交给用户确认后贴回，"
                 "不要由模型自行推断或重复使用"])


# ===========================================================================
# 写：place_order（闸4）
# ===========================================================================
@mcp.tool()
def place_order(intent_id: str, confirm_token: str) -> str:
    """**真正下单**（dry_run=1 时只写本地 orders 表）。这是下单的第二步。

    Args:
        intent_id: prepare_order 返回的意图 id。
        confirm_token: prepare_order 返回的 6 位数字，**必须由用户从对话中提供**。

    闸门：token 存在性 → 时效（2 分钟）→ 是否已消费 → token 匹配 → 重算限额与涨跌停。
    """
    tool = "place_order"
    try:
        con = _conn()
    except gates.GateReject as exc:
        return _reject(tool, exc)
    try:
        now = int(time.time())
        try:
            intent = gates.consume_intent(con, intent_id=intent_id,
                                          confirm_token=confirm_token, now=now)
        except gates.GateReject as exc:
            return _reject(tool, exc)

        try:
            gates.check_risk_review(con, intent["risk_review_id"],
                                    "signal", intent.get("signal_id") or None)
        except gates.GateReject as exc:
            con.execute("UPDATE order_intents SET status='rejected', reject_reason=? "
                        "WHERE intent_id=?", (exc.code, intent["intent_id"]))
            con.commit()
            return _reject(tool, exc)

        sym = intent["symbol"]
        meta = _q("SELECT board, is_st, lot_size, delist_date FROM instruments WHERE symbol=?",
                  (sym,))
        lot_size = (meta[0]["lot_size"] if meta else 100) or 100
        board = (meta[0]["board"] if meta else None) or nz.infer_board(sym)
        is_st = bool(meta[0]["is_st"]) if meta else False
        delisted = bool(meta[0]["delist_date"]) if meta else False
        bars = _q("""SELECT trade_date, close, pre_close FROM bars_daily
                     WHERE symbol=? ORDER BY trade_date DESC LIMIT 2""", (sym,))
        pre_close = None
        if bars:
            pre_close = bars[0].get("pre_close") or (bars[1].get("close") if len(bars) > 1 else None)
        limit_up = limit_down = None
        if pre_close:
            limit_up, limit_down = nz.limit_prices(
                pre_close, nz.price_limit_pct(board, is_st))

        try:
            amount = gates.check_limits(
                qty=int(intent["qty"]), price=float(intent["price"] or 0),
                lot_size=int(lot_size), max_order_amount=MAX_ORDER_AMOUNT,
                limit_up=limit_up, limit_down=limit_down, is_suspended=delisted,
                side=intent["side"], order_type=intent["order_type"])
        except gates.GateReject as exc:
            con.execute("UPDATE order_intents SET status='rejected', reject_reason=? "
                        "WHERE intent_id=?", (exc.code, intent["intent_id"]))
            con.commit()
            return _reject(tool, exc)

        order_id = f"ORD-{now}-{intent['intent_id'].split('-')[-1][:8]}"
        costs = gates.estimate_costs(side=intent["side"],
                                     price=float(intent["price"] or 0),
                                     qty=int(intent["qty"]))
        status = "dry_run"
        xt_order_id = None
        warnings: list[str] = []

        if not DRY_RUN:
            trader = _ensure_trader()
            if trader is None:
                con.execute("UPDATE order_intents SET status='rejected', reject_reason=? "
                            "WHERE intent_id=?", ("TRADER_UNAVAILABLE", intent["intent_id"]))
                con.commit()
                return _err(tool, "TRADER_UNAVAILABLE",
                            _trader_state.get("error", ""),
                            hint="确认码已被消费；问题解决后需重新 prepare_order")
            try:
                from xtquant import xtconstant
                code = sym.split(".")[0]
                side_const = (xtconstant.STOCK_BUY if intent["side"] == "buy"
                              else xtconstant.STOCK_SELL)
                price_type = xtconstant.FIX_PRICE
                xt_order_id = trader.order_stock(
                    _trader_state["account"], f"{code}.{sym.split('.')[1]}", side_const,
                    int(intent["qty"]), price_type, float(intent["price"] or 0),
                    "quant-agent", "quant-agent")
                if xt_order_id is None or xt_order_id < 0:
                    status = "failed"
                    warnings.append(f"order_stock 返回 {xt_order_id}，报单失败")
                else:
                    status = "submitted"
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                warnings.append(f"报单异常：{type(exc).__name__}: {exc}")
        else:
            warnings.append("dry_run=1：仅写本地 orders 表，未向券商报单")

        gates.record_order(con, order_id=order_id, intent=intent, costs=costs,
                           is_dry_run=DRY_RUN, xt_order_id=xt_order_id,
                           status=status, now=now)
    finally:
        con.close()
    return _ok(tool, {
        "order_id": order_id, "intent_id": intent["intent_id"],
        "status": status, "is_dry_run": DRY_RUN, "xt_order_id": xt_order_id,
        "symbol": sym, "side": intent["side"], "qty": int(intent["qty"]),
        "price": intent.get("price"), "est_amount": amount,
        "estimated_costs": costs,
        "next_step": ("dry_run 演练完成：orders 表已记录，券商无报单"
                      if DRY_RUN else "已报单，用 get_orders / get_trades 跟踪委托与成交"),
    }, warnings=warnings)


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """撤单（dry_run 下仅把本地订单置为 cancelled）。"""
    tool = "cancel_order"
    if not order_id:
        return _err(tool, "BAD_ARGUMENT", "order_id 必填")
    rows = _q("SELECT order_id, xt_order_id, status, is_dry_run, symbol, side FROM orders WHERE order_id=?",
              (order_id,))
    if not rows:
        return _err(tool, "NOT_FOUND", f"orders 中不存在 {order_id}")
    order = rows[0]
    if order["status"] in ("filled", "cancelled", "rejected", "failed"):
        return _err(tool, "NOT_CANCELLABLE",
                    f"订单状态为 {order['status']}，不可撤销")
    if CANCEL_SELL_ONLY and order["side"] == "buy" and not DRY_RUN:
        return _err(tool, "CANCEL_SELL_ONLY",
                    "当前配置只允许撤销卖单（防误撤买单）",
                    hint="确需撤销买单请人工在 QMT 客户端操作，或设 QUANT_CANCEL_SELL_ONLY=0")

    warnings: list[str] = []
    if DRY_RUN or not order["xt_order_id"]:
        warnings.append("dry_run 或本地订单：只更新本地状态，未向券商撤单")
    else:
        trader = _ensure_trader()
        if trader is None:
            return _err(tool, "TRADER_UNAVAILABLE", _trader_state.get("error", ""))
        try:
            result = trader.cancel_order_stock(_trader_state["account"],
                                               int(order["xt_order_id"]))
            if result != 0:
                return _err(tool, "CANCEL_FAILED", f"cancel_order_stock 返回 {result}")
        except Exception as exc:  # noqa: BLE001
            return _err(tool, "CANCEL_FAILED", f"撤单异常：{exc}")

    con = _conn()
    try:
        con.execute("UPDATE orders SET status='cancelled', "
                    "updated_at=datetime('now','localtime') WHERE order_id=?", (order_id,))
        con.commit()
    finally:
        con.close()
    return _ok(tool, {"order_id": order_id, "status": "cancelled"}, warnings=warnings)


if __name__ == "__main__":
    mcp.run()
