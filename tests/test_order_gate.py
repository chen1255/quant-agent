# -*- coding: utf-8 -*-
"""下单四道闸的离线回归测试（**不联网、不需要 QMT、不需要 xtquant**）。

这是本项目最关键的一组测试：闸门一旦失效，「模型可以自己下单」这件事就成立了。
覆盖 plan 里点名的五种拒绝：确认码 TTL 过期、重复消费、缺 risk_review_id、
verdict 非通过、dry_run 不报单。
"""

from __future__ import annotations

import pytest

T0 = 1_767_571_200          # 2026-01-05 00:00:00 UTC 附近的固定时间戳，便于断言


# ===========================================================================
# 闸2：风控凭证
# ===========================================================================
def test_gate2_missing_risk_review_id_is_rejected(gates_mod, db):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_risk_review(db, "", "signal", "SIG-1")
    assert exc.value.code == "RISK_REVIEW_REQUIRED"


def test_gate2_unknown_review_id_is_rejected(gates_mod, db):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_risk_review(db, "RR-DOES-NOT-EXIST", "signal", "SIG-1")
    assert exc.value.code == "RISK_REVIEW_NOT_FOUND"


def test_gate2_verdict_not_pass_is_rejected(gates_mod, db, review_reject):
    """verdict='不建议执行' 必须被拒 —— 只有「通过」才是可执行档。"""
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_risk_review(db, review_reject, "signal", "SIG-20260105-02")
    assert exc.value.code == "RISK_REVIEW_REJECTED"
    assert "不建议执行" in exc.value.message


def test_gate2_target_mismatch_is_rejected(gates_mod, db, review_pass):
    """不能拿 A 标的的复核结论去下 B 标的的单。"""
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_risk_review(db, review_pass, "signal", "SIG-OTHER")
    assert exc.value.code == "RISK_REVIEW_TARGET_MISMATCH"


def test_gate2_pass_review_is_accepted(gates_mod, db, review_pass):
    rec = gates_mod.check_risk_review(db, review_pass, "signal", "SIG-20260105-01")
    assert rec["verdict"] == "通过"


# ===========================================================================
# 闸3：确认码
# ===========================================================================
def test_gate3_create_intent_requires_risk_review_id(gates_mod, db, instrument):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                price=3.80, risk_review_id="", now=T0)
    assert exc.value.code == "RISK_REVIEW_REQUIRED"


def test_gate3_token_is_6_digits_and_ttl_120s(gates_mod, db, instrument, review_pass):
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    token = intent["confirm_token"]
    assert len(token) == 6 and token.isdigit()
    assert intent["expires_at"] - T0 == gates_mod.CONFIRM_TOKEN_TTL == 120
    row = db.execute("SELECT status, confirm_token FROM order_intents WHERE intent_id=?",
                     (intent["intent_id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["confirm_token"] == token


def test_gate3_expired_token_is_rejected(gates_mod, db, instrument, review_pass):
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                 confirm_token=intent["confirm_token"],
                                 now=T0 + gates_mod.CONFIRM_TOKEN_TTL + 1)
    assert exc.value.code == "INTENT_EXPIRED"
    row = db.execute("SELECT status FROM order_intents WHERE intent_id=?",
                     (intent["intent_id"],)).fetchone()
    assert row["status"] == "expired"


def test_gate3_token_is_single_use(gates_mod, db, instrument, review_pass):
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                             confirm_token=intent["confirm_token"], now=T0 + 5)
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                 confirm_token=intent["confirm_token"], now=T0 + 6)
    assert exc.value.code == "INTENT_ALREADY_CONSUMED"
    row = db.execute("SELECT status, consumed_at FROM order_intents WHERE intent_id=?",
                     (intent["intent_id"],)).fetchone()
    assert row["status"] == "consumed" and row["consumed_at"] == T0 + 5


def test_gate3_wrong_token_is_rejected_and_not_consumed(gates_mod, db, instrument, review_pass):
    """猜错确认码不能把码消耗掉（否则用户贴对了也用不了）。"""
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    wrong = "000000" if intent["confirm_token"] != "000000" else "111111"
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                 confirm_token=wrong, now=T0 + 5)
    assert exc.value.code == "TOKEN_MISMATCH"
    row = db.execute("SELECT status FROM order_intents WHERE intent_id=?",
                     (intent["intent_id"],)).fetchone()
    assert row["status"] == "pending"
    # 正确码仍然可用
    gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                             confirm_token=intent["confirm_token"], now=T0 + 7)


def test_gate3_non_numeric_token_is_rejected(gates_mod, db, instrument, review_pass):
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    for bad in ("12345", "1234567", "abcdef", ""):
        with pytest.raises(gates_mod.GateReject) as exc:
            gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                     confirm_token=bad, now=T0 + 5)
        assert exc.value.code == "BAD_ARGUMENT"


def test_gate3_expired_pending_intents_are_swept(gates_mod, db, instrument, review_pass):
    """同一标的的新意图应把旧的过期 pending 意图清成 expired，避免过期码被翻出来用。"""
    old = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                  price=3.80, risk_review_id=review_pass, now=T0)
    gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                            price=3.80, risk_review_id=review_pass,
                            now=T0 + gates_mod.CONFIRM_TOKEN_TTL + 10)
    row = db.execute("SELECT status FROM order_intents WHERE intent_id=?",
                     (old["intent_id"],)).fetchone()
    assert row["status"] == "expired"


# ===========================================================================
# 闸4：下单前重校验
# ===========================================================================
def test_gate4_order_amount_over_limit_is_rejected(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_limits(qty=100000, price=3.80, lot_size=100,
                               max_order_amount=100000, side="buy")
    assert exc.value.code == "ORDER_AMOUNT_EXCEEDED"


def test_gate4_lot_size_violation_is_rejected(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_limits(qty=150, price=3.80, lot_size=100, side="buy")
    assert exc.value.code == "LOT_SIZE_VIOLATION"


def test_gate4_suspended_symbol_is_rejected(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_limits(qty=100, price=3.80, is_suspended=True, side="buy")
    assert exc.value.code == "SYMBOL_SUSPENDED"


def test_gate4_price_beyond_limit_up_is_rejected(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_limits(qty=100, price=4.35, lot_size=100,
                               limit_up=4.29, limit_down=3.51, side="buy")
    assert exc.value.code == "PRICE_ABOVE_LIMIT_UP"


def test_gate4_market_order_disabled_by_default(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_limits(qty=100, price=0, order_type="market", side="buy")
    assert exc.value.code == "MARKET_ORDER_DISABLED"


def test_gate4_position_cap_is_enforced(gates_mod):
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.check_position_cap(target_value=50_000, total_asset=100_000,
                                     max_position_pct=0.30)
    assert exc.value.code == "POSITION_CAP_EXCEEDED"
    gates_mod.check_position_cap(target_value=29_000, total_asset=100_000,
                                 max_position_pct=0.30)   # 不抛即通过


# ===========================================================================
# dry_run：不报单，只落库
# ===========================================================================
def test_dry_run_writes_order_without_broker_id(gates_mod, db, instrument, review_pass):
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass,
                                     signal_id="SIG-20260105-01", now=T0)
    consumed = gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                        confirm_token=intent["confirm_token"], now=T0 + 3)
    costs = gates_mod.estimate_costs(side=consumed["side"], price=consumed["price"],
                                     qty=consumed["qty"])
    result = gates_mod.record_order(db, order_id="ORD-1", intent=consumed, costs=costs,
                                    is_dry_run=True, now=T0 + 3)

    assert result["status"] == "dry_run"
    assert result["is_dry_run"] is True
    assert result["xt_order_id"] is None
    row = db.execute("SELECT status, is_dry_run, xt_order_id, fee, tax FROM orders "
                     "WHERE order_id='ORD-1'").fetchone()
    assert row["status"] == "dry_run"
    assert row["is_dry_run"] == 1
    assert row["xt_order_id"] is None          # 没有券商委托号 = 没有真报单


def test_dry_run_refuses_to_carry_broker_order_id(gates_mod, db, instrument, review_pass):
    """兜底断言：dry_run 通路上不允许出现券商委托号（防代码分支走错）。"""
    intent = gates_mod.create_intent(db, symbol=instrument, side="buy", qty=100,
                                     price=3.80, risk_review_id=review_pass, now=T0)
    consumed = gates_mod.consume_intent(db, intent_id=intent["intent_id"],
                                        confirm_token=intent["confirm_token"], now=T0 + 3)
    costs = gates_mod.estimate_costs(side="buy", price=3.80, qty=100)
    with pytest.raises(gates_mod.GateReject) as exc:
        gates_mod.record_order(db, order_id="ORD-2", intent=consumed, costs=costs,
                               is_dry_run=True, xt_order_id=998877)
    assert exc.value.code == "DRY_RUN_LEAK"


# ===========================================================================
# 费用模型：与 backtests.cost_model_json 必须同口径
# ===========================================================================
def test_cost_model_min_commission_and_stamp_tax(gates_mod):
    buy = gates_mod.estimate_costs(side="buy", price=1.00, qty=100)     # 名义 100 元
    assert buy["commission"] == 5.0                                     # 触底最低 5 元
    assert buy["stamp_tax"] == 0.0                                      # 买入不收印花税

    buy_big = gates_mod.estimate_costs(side="buy", price=10.00, qty=20000)  # 名义 200000 元
    assert buy_big["commission"] == pytest.approx(50.0, abs=1e-6)        # 万 2.5
    assert buy_big["stamp_tax"] == 0.0

    sell = gates_mod.estimate_costs(side="sell", price=10.00, qty=20000)  # 名义 200000 元
    assert sell["commission"] == pytest.approx(50.0, abs=1e-6)           # 万 2.5
    assert sell["stamp_tax"] == pytest.approx(100.0, abs=1e-6)           # 卖出 0.05%
    assert sell["transfer_fee"] == pytest.approx(2.0, abs=1e-6)          # 过户费 0.001%
    assert sell["total"] == pytest.approx(152.0, abs=1e-6)
