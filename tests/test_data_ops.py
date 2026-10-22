# -*- coding: utf-8 -*-
"""quant-data 口径与错误包装的离线测试（**不联网、不依赖 akshare**）。

口径错误是量化里最贵的一类 bug：复权混用、换手率量纲错、代码归一化错，
都能让回测在「看起来正常」的情况下得出完全错误的结论。
"""

from __future__ import annotations

import pytest


# ===========================================================================
# 代码归一化
# ===========================================================================
@pytest.mark.parametrize("raw,expected", [
    ("600519", "600519.SH"),
    ("600519.SH", "600519.SH"),
    ("600519.sh", "600519.SH"),
    ("SH600519", "600519.SH"),
    ("sh.600519", "600519.SH"),
    ("510300", "510300.SH"),
    ("159915", "159915.SZ"),
    ("300750", "300750.SZ"),
    ("688981", "688981.SH"),
    ("430047", "430047.BJ"),
    ("000001", "000001.SZ"),
])
def test_normalize_symbol(normalize_mod, raw, expected):
    assert normalize_mod.normalize_symbol(raw) == expected


def test_normalize_symbol_explicit_exchange_wins(normalize_mod):
    """000001 在沪深两市都存在（平安银行 / 上证指数），显式指定交易所必须优先。"""
    assert normalize_mod.normalize_symbol("000001", "SH") == "000001.SH"
    assert normalize_mod.normalize_symbol("000001", "SZ") == "000001.SZ"


@pytest.mark.parametrize("bad", ["", "  ", "60051", "6005190", "ABC123", "6005AB", None])
def test_normalize_symbol_rejects_invalid(normalize_mod, bad):
    with pytest.raises(ValueError):
        normalize_mod.normalize_symbol(bad)


def test_split_symbol_roundtrip(normalize_mod):
    assert normalize_mod.split_symbol("510300.SH") == ("510300", "SH")
    assert normalize_mod.split_symbol(600519) == ("600519", "SH")


# ===========================================================================
# 板块与涨跌幅限制
# ===========================================================================
@pytest.mark.parametrize("symbol,asset_type,board", [
    ("600519", "stock", "main"),
    ("000001", "stock", "main"),
    ("300750", "stock", "chinext"),
    ("688981", "stock", "star"),
    ("430047", "stock", "bse"),
    ("510300", "etf", "etf"),
    ("159915", "etf", "etf"),
    ("000300", "index", "index"),
])
def test_infer_board(normalize_mod, symbol, asset_type, board):
    assert normalize_mod.infer_board(symbol, asset_type) == board


def test_price_limit_pct_matches_exchange_rules(normalize_mod):
    assert normalize_mod.price_limit_pct("main") == 0.10
    assert normalize_mod.price_limit_pct("main", is_st=True) == 0.05   # ST 主板收窄
    assert normalize_mod.price_limit_pct("chinext") == 0.20
    assert normalize_mod.price_limit_pct("star") == 0.20
    assert normalize_mod.price_limit_pct("bse") == 0.30
    assert normalize_mod.price_limit_pct("etf") == 0.10
    # 创业板/科创板 ST 仍是 20%，不因 ST 收窄
    assert normalize_mod.price_limit_pct("chinext", is_st=True) == 0.20


def test_limit_prices_round_to_cent(normalize_mod):
    up, down = normalize_mod.limit_prices(3.90, 0.10)
    assert (up, down) == (4.29, 3.51)
    up, down = normalize_mod.limit_prices(100.00, 0.05)
    assert (up, down) == (105.00, 95.00)


def test_limit_prices_reject_non_positive_pre_close(normalize_mod):
    with pytest.raises(ValueError):
        normalize_mod.limit_prices(0, 0.10)


# ===========================================================================
# 复权换算
# ===========================================================================
def test_convert_price_modes(normalize_mod):
    # 原始 10 元，累计因子 2.0，最新因子 2.5（即该标的上市以来分红送转）
    assert normalize_mod.convert_price(10.0, 2.0, 2.5, "none") == 10.0
    assert normalize_mod.convert_price(10.0, 2.0, 2.5, "hfq") == 20.0
    assert normalize_mod.convert_price(10.0, 2.0, 2.5, "qfq") == pytest.approx(8.0)


def test_convert_price_defaults_and_none(normalize_mod):
    assert normalize_mod.convert_price(None, 2.0, 2.5, "qfq") is None
    # 因子缺失按 1.0 处理，绝不抛异常（宁可不动，也不臆造因子）
    assert normalize_mod.convert_price(10.0, None, None, "hfq") == 10.0
    assert normalize_mod.convert_price(10.0, 1.0, None, "qfq") == 10.0


def test_convert_price_rejects_unknown_mode(normalize_mod):
    with pytest.raises(ValueError):
        normalize_mod.convert_price(10.0, 1.0, 1.0, "adjusted")


# ===========================================================================
# 上游行归一化
# ===========================================================================
def test_normalize_bar_row_chinese_columns(normalize_mod):
    row = {"日期": "2026-01-05", "开盘": "3.801", "最高": "3.850", "最低": "3.790",
           "收盘": "3.800", "成交量": "1,000,000", "成交额": "3800000", "换手率": "12.3"}
    bar = normalize_mod.normalize_bar_row(row, "510300", source="akshare")
    assert bar["symbol"] == "510300.SH"
    assert bar["trade_date"] == "2026-01-05"
    assert bar["close"] == 3.800
    assert bar["volume"] == 1_000_000
    assert bar["turnover_rate"] == pytest.approx(0.123)    # 百分数 → 小数
    assert bar["adj_factor"] == 1.0
    assert bar["source"] == "akshare"


def test_normalize_bar_row_english_columns_and_dashes(normalize_mod):
    row = {"trade_date": "2026-01-06T00:00:00", "open": 3.8, "high": 3.9, "low": 3.7,
           "close": 3.85, "volume": "-", "amount": None, "turnover_rate": "0.85",
           "adj_factor": "1.5"}
    bar = normalize_mod.normalize_bar_row(row, "600519.SH")
    assert bar["trade_date"] == "2026-01-06"
    assert bar["volume"] is None and bar["amount"] is None
    assert bar["turnover_rate"] == pytest.approx(0.85)     # ≤1.5 视为已是小数
    assert bar["adj_factor"] == 1.5


def test_normalize_bar_row_rejects_bad_date(normalize_mod):
    with pytest.raises(ValueError):
        normalize_mod.normalize_bar_row({"date": "2026/01/05", "close": 3.8}, "510300")


def test_latest_factor_from_takes_last_row(normalize_mod):
    rows = [{"adj_factor": 1.0}, {"adj_factor": 1.8}, {"adj_factor": 2.5}]
    assert normalize_mod.latest_factor_from(rows) == 2.5
    assert normalize_mod.latest_factor_from([]) == 1.0


# ===========================================================================
# 错误信封（上游失败绝不能挂死或抛裸异常）
# ===========================================================================
def test_ok_envelope(normalize_mod):
    payload = normalize_mod.ok("get_instruments", [{"symbol": "510300.SH"}],
                               total=1, warnings=["示例"])
    assert payload["ok"] is True
    assert payload["tool"] == "get_instruments"
    assert payload["total"] == 1
    assert payload["warnings"] == ["示例"]


def test_err_envelope_is_structured(normalize_mod):
    payload = normalize_mod.err("get_daily_bars", normalize_mod.E_UPSTREAM_TIMEOUT,
                                "上游超时", hint="稍后重试", details={"symbol": "510300.SH"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert payload["error"]["message"] == "上游超时"
    assert payload["error"]["hint"] == "稍后重试"
    assert payload["error"]["details"] == {"symbol": "510300.SH"}


def test_error_codes_are_stable_strings(normalize_mod):
    """错误码是技能文档里引用到的契约，改名会让文档失效。"""
    assert normalize_mod.E_UPSTREAM_TIMEOUT == "UPSTREAM_TIMEOUT"
    assert normalize_mod.E_UPSTREAM_FAILED == "UPSTREAM_FAILED"
    assert normalize_mod.E_EMPTY_DB == "EMPTY_DB"
    assert normalize_mod.E_NOT_FOUND == "NOT_FOUND"
    assert normalize_mod.E_BAD_ARGUMENT == "BAD_ARGUMENT"


# ===========================================================================
# 与闸门模块的错误码一致性
# ===========================================================================
def test_gate_reject_codes_cover_documented_ones(gates_mod):
    """live-trading 技能文档里列出的拒绝码必须都能由 GateReject 产出。"""
    documented = [
        "RISK_REVIEW_REQUIRED", "RISK_REVIEW_NOT_FOUND", "RISK_REVIEW_REJECTED",
        "RISK_REVIEW_TARGET_MISMATCH", "INTENT_NOT_FOUND", "INTENT_EXPIRED",
        "INTENT_ALREADY_CONSUMED", "TOKEN_MISMATCH", "ORDER_AMOUNT_EXCEEDED",
        "LOT_SIZE_VIOLATION", "SYMBOL_SUSPENDED", "PRICE_ABOVE_LIMIT_UP",
        "PRICE_BELOW_LIMIT_DOWN", "POSITION_CAP_EXCEEDED", "BAD_ARGUMENT",
    ]
    source = (gates_mod.GateReject.__module__ and
              __import__("pathlib").Path(gates_mod.__file__).read_text(encoding="utf-8"))
    for code in documented:
        assert f'"{code}"' in source, f"gates.py 未定义文档中声明的拒绝码：{code}"
