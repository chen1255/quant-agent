"""trade-gateway 的下单闸门逻辑（纯函数，可用内存 SQLite 离线单测）。

**这是整个项目最重要的一个文件。** 提示词层写「必须先复核」是可以被模型绕过的，
所以四道闸必须落在 MCP（代码）层：

  闸1 默认关闭  —— MCP enabled=false + QUANT_TRADE_DRY_RUN 默认 1（见 server.py / mcp.example.json）
  闸2 风控凭证  —— prepare_order 必须给 risk_review_id，且该记录 verdict='通过'
  闸3 人工确认码 —— 6 位数字，2 分钟 TTL，单次消费，落 order_intents
  闸4 下单重校验 —— place_order 复核 token 时效/消费状态 + 风控 id + 限额 + 涨跌停

本模块不导入 xtquant、不联网，可被 tests/test_order_gate.py 直接导入。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import string
from typing import Any, Optional

PASS_VERDICT = "通过"
ACCEPT_VERDICTS = ("通过", "补充措施后可执行")   # 只有「通过」允许直接下单
CONFIRM_TOKEN_TTL = 120                          # 秒；2 分钟
DEFAULT_MAX_ORDER_AMOUNT = 100_000.0             # 单笔上限（元）
DEFAULT_MAX_POSITION_PCT = 0.30                  # 单标的最大仓位占比


class GateReject(Exception):
    """闸门拒绝。code 用于给调用方区分拦截原因（不改判为异常吞掉）。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


# ---------------------------------------------------------------------------
# 闸2：风控凭证
# ---------------------------------------------------------------------------
def check_risk_review(con: sqlite3.Connection, review_id: str,
                      target_type: str = "signal",
                      target_id: Optional[str] = None) -> dict:
    """校验 risk_reviews 记录可作为下单凭证。

    要求：① review_id 非空；② 记录存在；③ verdict 落在 ``ACCEPT_VERDICTS``；
         ④ 若给定 target_id，则记录必须指向同一标的（防「拿 A 的复核下 B 的单」）。
    返回该记录 dict；不满足则抛 GateReject。
    """
    if not review_id or not str(review_id).strip():
        raise GateReject(
            "RISK_REVIEW_REQUIRED",
            "缺少 risk_review_id：下单前必须先由 risk-reviewer 出具复核结论",
            "调用 task(risk-reviewer) 复核后，把 review_id 一并带入 prepare_order")

    row = con.execute(
        """SELECT review_id, target_type, target_id, verdict, findings_json,
                  reviewed_at, reviewer
           FROM risk_reviews WHERE review_id=?""",
        (str(review_id).strip(),)).fetchone()
    if row is None:
        raise GateReject("RISK_REVIEW_NOT_FOUND",
                         f"risk_reviews 中不存在 review_id={review_id}",
                         "复核结论必须真实落库；不要伪造 review_id")

    rec = dict(row)
    if rec["verdict"] not in ACCEPT_VERDICTS:
        raise GateReject(
            "RISK_REVIEW_REJECTED",
            f"复核结论为「{rec['verdict']}」，不是可执行档（允许：{'/'.join(ACCEPT_VERDICTS)}）",
            "按下单前复核的 findings 修改策略或参数后重新复核，不要跳过闸门")

    if target_id and str(rec.get("target_id") or "") != str(target_id):
        raise GateReject(
            "RISK_REVIEW_TARGET_MISMATCH",
            f"复核凭证目标为 {rec.get('target_type')}:{rec.get('target_id')}，"
            f"与本次下单目标 {target_type}:{target_id} 不一致",
            "同一复核结论只能用于其审查过的目标")
    return rec


# ---------------------------------------------------------------------------
# 闸3：确认码
# ---------------------------------------------------------------------------
def _gen_token() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


def create_intent(con: sqlite3.Connection, *, symbol: str, side: str, qty: int,
                  price: Optional[float], order_type: str = "limit",
                  risk_review_id: str, now: int, ttl: int = CONFIRM_TOKEN_TTL,
                  strategy_id: Optional[str] = None,
                  signal_id: Optional[str] = None,
                  account_id: Optional[str] = None,
                  est_amount: Optional[float] = None,
                  intent_id: Optional[str] = None) -> dict:
    """创建下单意图并生成确认码（闸3）。

    每次调用都**先清理**该标的的过期 pending 意图，避免过期码堆积后被复用。
    """
    if not symbol:
        raise GateReject("BAD_ARGUMENT", "symbol 不能为空")
    if side not in ("buy", "sell"):
        raise GateReject("BAD_ARGUMENT", "side 只能是 buy / sell")
    if not isinstance(qty, int) or qty <= 0:
        raise GateReject("BAD_ARGUMENT", "qty 必须是正整数")
    if order_type not in ("limit", "market"):
        raise GateReject("BAD_ARGUMENT", "order_type 只能是 limit / market")
    if order_type == "limit" and not price:
        raise GateReject("BAD_ARGUMENT", "限价单必须给 price")
    if not risk_review_id:
        raise GateReject("RISK_REVIEW_REQUIRED", "生成确认码前必须先有 risk_review_id")

    iid = intent_id or f"INT-{now}-{secrets.token_hex(4).upper()}"
    token = _gen_token()
    expires_at = int(now) + int(ttl)
    payload = {"symbol": symbol, "side": side, "qty": qty, "price": price,
               "order_type": order_type, "strategy_id": strategy_id,
               "signal_id": signal_id, "risk_review_id": risk_review_id}

    con.execute(
        """UPDATE order_intents SET status='expired'
           WHERE COALESCE(status,'pending')='pending' AND expires_at<=?
             AND COALESCE(account_id,'')=COALESCE(?,'') AND symbol=? AND side=?""",
        (int(now), account_id, symbol, side))
    con.execute(
        """INSERT INTO order_intents
             (intent_id, created_at, expires_at, consumed_at, status, symbol, side,
              order_type, price, qty, est_amount, strategy_id, signal_id,
              risk_review_id, confirm_token, account_id, payload_json, reject_reason)
           VALUES (?,?,?,NULL,'pending',?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (iid, int(now), expires_at, symbol, side, order_type, price, qty,
         est_amount, strategy_id, signal_id, risk_review_id, token, account_id,
         json.dumps(payload, ensure_ascii=False)))
    con.commit()
    return {"intent_id": iid, "confirm_token": token, "expires_at": expires_at,
            "ttl_seconds": int(ttl), "payload": payload}


def consume_intent(con: sqlite3.Connection, *, intent_id: str, confirm_token: str,
                   now: int) -> dict:
    """消费确认码（闸3+闸4前置）：校验存在性 → 时效 → 消费状态 → token 匹配。

    成功后原子地把 status 置为 consumed 并写 consumed_at；任何一步不通过都抛 GateReject，
    **且不会消耗 token**（除「重复消费」这种终态拒绝外，其余情况用户可重试）。
    """
    if not intent_id or not confirm_token:
        raise GateReject("BAD_ARGUMENT", "intent_id 与 confirm_token 均为必填",
                         "确认码必须由用户从对话中贴回，不能由模型自行猜测")
    token = str(confirm_token).strip()
    if not (len(token) == 6 and token.isdigit()):
        raise GateReject("BAD_ARGUMENT", "confirm_token 必须是 6 位数字")

    row = con.execute(
        """SELECT intent_id, created_at, expires_at, consumed_at, status, symbol,
                  side, order_type, price, qty, est_amount, risk_review_id,
                  confirm_token, strategy_id, signal_id, account_id
           FROM order_intents WHERE intent_id=?""",
        (str(intent_id),)).fetchone()
    if row is None:
        raise GateReject("INTENT_NOT_FOUND", f"找不到 intent_id={intent_id}")

    rec = dict(row)
    if rec.get("consumed_at") is not None or rec.get("status") == "consumed":
        raise GateReject("INTENT_ALREADY_CONSUMED",
                         f"确认码已于 {rec['consumed_at']} 被消费，不可复用",
                         "需要再次下单请重新 prepare_order 生成新确认码")
    if rec.get("status") == "rejected":
        raise GateReject("INTENT_REJECTED", "该下单意图已被拒绝（见 reject_reason）")
    if int(rec["expires_at"]) <= int(now):
        con.execute("UPDATE order_intents SET status='expired' WHERE intent_id=?",
                    (str(intent_id),))
        con.commit()
        raise GateReject("INTENT_EXPIRED",
                         f"确认码已过期（有效期 {int(rec['expires_at']) - int(rec['created_at'])} 秒）",
                         "重新 prepare_order 获取新确认码")
    if not secrets.compare_digest(str(rec["confirm_token"]), token):
        raise GateReject("TOKEN_MISMATCH", "确认码不正确",
                         "请向用户索取 prepare_order 返回的 6 位数字，禁止模型自行编造")

    cur = con.execute(
        """UPDATE order_intents
           SET consumed_at=?, status='consumed'
           WHERE intent_id=? AND consumed_at IS NULL AND status='pending'""",
        (int(now), str(intent_id)))
    if cur.rowcount < 1:
        con.rollback()
        raise GateReject("INTENT_ALREADY_CONSUMED", "确认码并发消费冲突，已拒绝")
    con.commit()
    return rec


# ---------------------------------------------------------------------------
# 闸4：限额与可成交性
# ---------------------------------------------------------------------------
def check_limits(*, qty: int, price: float, lot_size: int = 100,
                 max_order_amount: float = DEFAULT_MAX_ORDER_AMOUNT,
                 limit_up: Optional[float] = None,
                 limit_down: Optional[float] = None,
                 is_suspended: bool = False, side: str = "buy",
                 allow_market_order: bool = False,
                 order_type: str = "limit") -> float:
    """校验单笔限额、最小交易单位、涨跌停与停牌。返回估算金额。"""
    if is_suspended:
        raise GateReject("SYMBOL_SUSPENDED", "标的当日停牌，不可交易")
    if order_type == "market" and not allow_market_order:
        raise GateReject("MARKET_ORDER_DISABLED",
                         "本期未开放市价单（无法在下单前校验涨跌停边界）",
                         "改用限价单，价格由用户确认")
    if lot_size and lot_size > 0 and qty % lot_size != 0:
        raise GateReject("LOT_SIZE_VIOLATION",
                         f"数量 {qty} 不是最小交易单位 {lot_size} 的整数倍")
    amount = float(qty) * float(price or 0)
    if amount > max_order_amount:
        raise GateReject("ORDER_AMOUNT_EXCEEDED",
                         f"单笔金额 {amount:,.2f} 元超过上限 {max_order_amount:,.2f} 元",
                         "拆单或调高 strategies.risk_limits_json 中的 max_order_amount（需人工确认）")
    if side == "buy" and limit_up and price and float(price) > float(limit_up) + 1e-9:
        raise GateReject("PRICE_ABOVE_LIMIT_UP",
                         f"买入价 {price} 高于涨停价 {limit_up}，报单会被交易所拒绝")
    if side == "sell" and limit_down and price and float(price) < float(limit_down) - 1e-9:
        raise GateReject("PRICE_BELOW_LIMIT_DOWN",
                         f"卖出价 {price} 低于跌停价 {limit_down}，报单会被交易所拒绝")
    return amount


def check_position_cap(*, target_value: float, total_asset: float,
                       max_position_pct: float = DEFAULT_MAX_POSITION_PCT) -> None:
    """单标的目标仓位上限校验（按「本次下单后」的目标市值计算）。"""
    if total_asset <= 0:
        raise GateReject("ACCOUNT_UNAVAILABLE", "账户总资产不可用，无法校验仓位上限")
    pct = target_value / total_asset
    if pct > max_position_pct + 1e-9:
        raise GateReject("POSITION_CAP_EXCEEDED",
                         f"下单后单标的占比 {pct:.1%} 超过上限 {max_position_pct:.1%}",
                         "减小下单数量或调整 risk_limits_json")


# ---------------------------------------------------------------------------
# 费用模型（与 backtests.cost_model_json 必须同口径）
# ---------------------------------------------------------------------------
def estimate_costs(*, side: str, price: float, qty: int,
                   commission_rate: float = 0.00025,
                   min_commission: float = 5.0,
                   stamp_tax_rate: float = 0.0005,
                   transfer_fee_rate: float = 0.00001,
                   slippage_rate: float = 0.0) -> dict:
    """估算佣金（含最低 5 元）/ 印花税（仅卖出）/ 过户费 / 滑点。"""
    notional = float(price) * int(qty)
    commission = max(notional * commission_rate, min_commission)
    tax = notional * stamp_tax_rate if side == "sell" else 0.0
    transfer = notional * transfer_fee_rate
    slippage = notional * slippage_rate
    total = commission + tax + transfer + slippage
    return {"notional": round(notional, 2), "commission": round(commission, 2),
            "stamp_tax": round(tax, 2), "transfer_fee": round(transfer, 2),
            "slippage": round(slippage, 2), "total": round(total, 2)}


def record_order(con: sqlite3.Connection, *, order_id: str, intent: dict,
                 costs: dict, is_dry_run: bool, xt_order_id=None,
                 status: Optional[str] = None, now: Optional[int] = None) -> dict:
    """把订单落库。

    ``is_dry_run=True`` 时状态固定为 ``dry_run`` 且 ``xt_order_id`` 必须为空 ——
    持仓与订单表是唯一的执行事实来源，dry_run 通路上不允许出现券商委托号。
    """
    if is_dry_run and xt_order_id:
        raise GateReject("DRY_RUN_LEAK",
                         "dry_run 模式不得携带券商委托号（说明有代码路径越过了 dry_run 分支）")
    final_status = status or ("dry_run" if is_dry_run else "submitted")
    con.execute(
        """INSERT INTO orders (order_id, intent_id, signal_id, strategy_id,
             account_id, symbol, side, order_type, price, qty, filled_qty,
             fee, tax, transfer_fee, status, is_dry_run, xt_order_id, submitted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
        (order_id, intent.get("intent_id"), intent.get("signal_id"),
         intent.get("strategy_id"), intent.get("account_id"), intent["symbol"],
         intent["side"], intent.get("order_type") or "limit", intent.get("price"),
         int(intent["qty"]), costs["commission"], costs["stamp_tax"],
         costs["transfer_fee"], final_status, 1 if is_dry_run else 0,
         str(xt_order_id) if xt_order_id else None,
         int(now) if now is not None else None))
    con.commit()
    return {"order_id": order_id, "status": final_status, "is_dry_run": bool(is_dry_run),
            "xt_order_id": str(xt_order_id) if xt_order_id else None}
