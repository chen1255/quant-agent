# -*- coding: utf-8 -*-
"""pytest 公共夹具：真实加载 schema.sql 建库，并按真实源码路径导入被测算模块。

刻意**不使用** pip 安装：``mcp-servers`` 目录里有连字符，不能当包名 import，
所以统一用 importlib 按文件路径加载，保证测的就是仓库里那份代码。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
QUANT_DATA_DIR = ROOT / "mcp-servers" / "quant-data"
TRADE_GATEWAY_DIR = ROOT / "mcp-servers" / "trade-gateway"


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def normalize_mod():
    """quant-data 的口径/错误包装纯函数层。"""
    return _load("qa_normalize", QUANT_DATA_DIR / "normalize.py")


@pytest.fixture(scope="session")
def gates_mod():
    """trade-gateway 的下单闸门层（不含 xtquant，可离线跑）。"""
    return _load("qa_gates", TRADE_GATEWAY_DIR / "gates.py")


@pytest.fixture()
def db(gates_mod, tmp_path):
    """用真实 schema.sql 建的临时库（保证表结构变更会立刻让测试失败）。"""
    schema = ROOT / "integrations" / "quant-db" / "schema.sql"
    con = sqlite3.connect(tmp_path / "quant_test.db")
    con.row_factory = sqlite3.Row
    con.executescript(schema.read_text(encoding="utf-8"))
    yield con
    con.close()


# ---------------------------------------------------------------------------
# 共用的种子：标的、复核结论
# ---------------------------------------------------------------------------
@pytest.fixture()
def instrument(db):
    db.execute(
        """INSERT INTO instruments (symbol, code, exchange, name, asset_type, board,
             list_date, lot_size, is_st, status)
           VALUES ('510300.SH','510300','SH','沪深300ETF','etf','etf','2012-05-28',100,0,'active')""")
    db.execute(
        """INSERT INTO bars_daily (symbol, trade_date, close, pre_close, volume, amount, adj_factor)
           VALUES ('510300.SH','2026-01-05',3.800,3.900,1000000,3800000,1.0)""")
    db.commit()
    return "510300.SH"


@pytest.fixture()
def review_pass(db):
    """一份 verdict='通过' 的复核记录（可作为下单凭证）。"""
    db.execute(
        """INSERT INTO risk_reviews (review_id, target_type, target_id, reviewer,
             verdict, findings_json, reviewed_at)
           VALUES ('RR-20260105-01','signal','SIG-20260105-01','risk-reviewer','通过',
                   '[{"dimension":"前视偏差","evidence":"已做信号后移1日对照，收益仍为正","verdict":"通过"}]',
                   1767571200)""")
    db.commit()
    return "RR-20260105-01"


@pytest.fixture()
def review_reject(db):
    """一份 verdict='不建议执行' 的复核记录 —— 必须被闸门拒绝。"""
    db.execute(
        """INSERT INTO risk_reviews (review_id, target_type, target_id, reviewer,
             verdict, findings_json, reviewed_at)
           VALUES ('RR-20260105-02','signal','SIG-20260105-02','risk-reviewer','不建议执行',
                   '[{"dimension":"交易成本","evidence":"cost_model_json 缺失","verdict":"不通过"}]',
                   1767571300)""")
    db.commit()
    return "RR-20260105-02"
