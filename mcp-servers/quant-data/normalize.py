"""quant-data MCP 的纯函数工具层（无网络、无第三方依赖，可离线单测）。

这里只放「口径」与「错误包装」两类逻辑：
  * 代码归一化、板块/涨跌幅推断、复权换算 —— 口径错了，后面所有因子与回测都是错的；
  * 统一错误信封 —— 上游（akshare/baostock）失败时必须返回结构化错误，而不是挂死。

tests/test_data_ops.py 直接导入本模块做离线断言。
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Optional

# --------------------------------------------------------------------------
# 1) 代码与交易所
# --------------------------------------------------------------------------
# A 股代码前缀 → 交易所（公开规则，不依赖任何数据源）
_SH_PREFIXES = ("60", "68", "51", "56", "58", "50", "11", "90", "110", "113", "118")
_SZ_PREFIXES = ("00", "30", "15", "16", "18", "12", "39")
_BJ_PREFIXES = ("43", "83", "87", "88", "92", "20")

_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})(?:[.\-]?(?P<ex>SH|SZ|BJ|sh|sz|bj))?$")
_INDEX_RE = re.compile(r"^(?P<code>\d{6})$")


def infer_exchange(code: str) -> str:
    """按 6 位数字代码前缀推断交易所；无法判断时抛 ValueError。"""
    if not re.fullmatch(r"\d{6}", code or ""):
        raise ValueError(f"非法 A 股代码：{code!r}（应为 6 位数字）")
    if code.startswith(_SH_PREFIXES):
        return "SH"
    if code.startswith(_SZ_PREFIXES):
        return "SZ"
    if code.startswith(_BJ_PREFIXES):
        return "BJ"
    raise ValueError(f"无法按前缀判断交易所：{code}")


def normalize_symbol(raw: str, exchange: Optional[str] = None) -> str:
    """归一化为 ``code.exchange``，如 ``600519.SH`` / ``510300.SH``。

    接受 ``600519`` / ``600519.SH`` / ``SH600519`` / ``sh.600519`` 等常见写法。
    显式传入 exchange 时以传入值为准（用于 000001.SZ 与 000001.SH 这类同号不同标的）。
    """
    if raw is None:
        raise ValueError("symbol 不能为空")
    text = str(raw).strip().upper().replace(" ", "")
    if not text:
        raise ValueError("symbol 不能为空")
    if exchange:
        ex = exchange.strip().upper()
        if ex not in ("SH", "SZ", "BJ"):
            raise ValueError(f"非法交易所：{exchange!r}")
    else:
        ex = ""

    if text.startswith(("SH", "SZ", "BJ")) and re.fullmatch(r"(SH|SZ|BJ)\.?\d{6}", text):
        ex, text = text[:2], text[2:].lstrip(".")
    else:
        m = _SYMBOL_RE.match(text)
        if not m:
            raise ValueError(f"无法解析代码：{raw!r}")
        if m.group("ex"):
            ex = ex or m.group("ex").upper()
        text = m.group("code")

    return f"{text}.{ex or infer_exchange(text)}"


def split_symbol(symbol: str) -> tuple[str, str]:
    """``600519.SH`` -> ``("600519", "SH")``。"""
    norm = normalize_symbol(symbol)
    code, ex = norm.split(".")
    return code, ex


def infer_board(symbol: str, asset_type: str = "stock") -> str:
    """推断板块（决定涨跌幅限制）。"""
    code, ex = split_symbol(symbol)
    if asset_type == "index":
        return "index"
    if asset_type in ("etf", "fund", "bond"):
        return "etf"
    if ex == "BJ" or code.startswith(("43", "83", "87", "88", "92")):
        return "bse"
    if code.startswith("688"):
        return "star"
    if code.startswith("300") or code.startswith("301"):
        return "chinext"
    return "main"


def price_limit_pct(board: str, is_st: bool = False) -> float:
    """涨跌幅限制（小数）。ST 仅在主板收窄到 5%；创业板/科创板 ST 仍为 20%。"""
    board = (board or "main").lower()
    if board in ("bse",):
        return 0.30
    if board in ("star", "chinext"):
        return 0.20
    if board == "etf":
        return 0.10
    return 0.05 if is_st else 0.10


def limit_prices(pre_close: float, pct: float) -> tuple[float, float]:
    """由前收与涨跌幅算 (涨停价, 跌停价)，交易所口径为四舍五入到分。"""
    if pre_close is None or pre_close <= 0:
        raise ValueError("pre_close 必须为正数")
    return (round(pre_close * (1 + pct) + 1e-9, 2), round(pre_close * (1 - pct) + 1e-9, 2))


# --------------------------------------------------------------------------
# 2) 复权口径
# --------------------------------------------------------------------------
def convert_price(price: Optional[float], adj_factor: Optional[float],
                  latest_factor: Optional[float], mode: str = "qfq") -> Optional[float]:
    """按统一口径换算价格。

    库里 ``bars_daily`` 存 ``adj_factor`` 为**累计复权因子（后复权口径）**：
      * hfq：price * adj_factor
      * qfq：price * adj_factor / latest_factor（latest_factor 为该标的最新累计因子）
      * none：原价

    量（volume/amount）恒为不复权口径，禁止把 qfq 价与 hfq 量混用。
    """
    if price is None:
        return None
    mode = (mode or "none").lower()
    if mode == "none":
        return float(price)
    f = 1.0 if adj_factor in (None, 0) else float(adj_factor)
    if mode == "hfq":
        return float(price) * f
    if mode == "qfq":
        lf = f if latest_factor in (None, 0) else float(latest_factor)
        return float(price) * f / lf
    raise ValueError(f"未知复权口径：{mode!r}（可选 none/qfq/hfq）")


# --------------------------------------------------------------------------
# 3) 上游行数据归一化
# --------------------------------------------------------------------------
_BAR_ALIASES = {
    "trade_date": ("trade_date", "date", "日期", "时间"),
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "vol", "成交量"),
    "amount": ("amount", "成交额"),
    "turnover_rate": ("turnover_rate", "换手率"),
    "adj_factor": ("adj_factor", "复权因子"),
    "source": ("source", "数据源"),
}


def _pick(row: Mapping[str, Any], field: str) -> Any:
    for key in _BAR_ALIASES.get(field, (field,)):
        if key in row and row[key] not in (None, "", "-"):
            return row[key]
    return None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, "", "-", "--"):
        return None
    try:
        f = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def normalize_bar_row(row: Mapping[str, Any], symbol: str,
                      source: str = "akshare") -> dict:
    """把上游一行日线归一化成 ``bars_daily`` 的行字典。

    中英文列名都认；换手率若是百分数（如 ``1.23%`` 或 ``1.23``）统一转小数。
    """
    trade_date = str(_pick(row, "trade_date") or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
        raise ValueError(f"缺交易日或格式非法：{row.get('date') or row.get('trade_date')!r}")

    turnover = _to_float(_pick(row, "turnover_rate"))
    if turnover is not None and turnover > 1.5:  # 换成小数；A股单日换手几乎不会 >150%
        turnover = turnover / 100.0

    return {
        "symbol": normalize_symbol(symbol),
        "trade_date": trade_date,
        "open": _to_float(_pick(row, "open")),
        "high": _to_float(_pick(row, "high")),
        "low": _to_float(_pick(row, "low")),
        "close": _to_float(_pick(row, "close")),
        "volume": _to_float(_pick(row, "volume")),
        "amount": _to_float(_pick(row, "amount")),
        "turnover_rate": turnover,
        "adj_factor": _to_float(_pick(row, "adj_factor")) or 1.0,
        "source": str(_pick(row, "source") or source),
    }


def latest_factor_from(rows) -> float:
    """取最后一行的累计复权因子作为 qfq 换算基准（rows 需按日期升序）。"""
    for row in reversed(list(rows)):
        f = _to_float(row.get("adj_factor") if isinstance(row, Mapping) else None)
        if f:
            return f
    return 1.0


# --------------------------------------------------------------------------
# 4) 统一返回信封
# --------------------------------------------------------------------------
ErrorCode = str


def ok(tool: str, data: Any = None, **extra: Any) -> dict:
    """成功信封。data 为空列表时也要显式给 warnings，避免「查不到」被当成「没有风险」。"""
    payload = {"ok": True, "tool": tool, "data": data}
    payload.update(extra)
    return payload


def err(tool: str, code: ErrorCode, message: str, hint: str = "",
        details: Any = None) -> dict:
    """结构化错误信封：绝不抛裸异常给 MCP 层（否则表现为客户端超时/连接断开）。"""
    payload: dict = {"ok": False, "tool": tool,
                     "error": {"code": code, "message": message}}
    if hint:
        payload["error"]["hint"] = hint
    if details is not None:
        payload["error"]["details"] = details
    return payload


# 常用错误码（供技能与文档引用，保持一致）
E_UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
E_UPSTREAM_FAILED = "UPSTREAM_FAILED"
E_EMPTY_DB = "EMPTY_DB"
E_NOT_FOUND = "NOT_FOUND"
E_BAD_ARGUMENT = "BAD_ARGUMENT"
E_PUB_DATE_UNAVAILABLE = "PUB_DATE_UNAVAILABLE"
E_NO_ADJ_FACTOR = "NO_ADJ_FACTOR"