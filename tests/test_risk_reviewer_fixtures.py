# -*- coding: utf-8 -*-
"""risk-reviewer 复核规则的回炉检查（**离线、不调用模型**）。

为什么要有这组测试：plan 里写明「若 3 个已知错误样例全被判通过，说明复核提示词太软、
需要回炉」。模型调用无法在 CI 里稳定复现，所以这里做两件事：

1. **提示词覆盖度断言**：直接读 `subagents/risk-reviewer.yaml` 的 system_prompt，
   断言 11 个复核维度、三档结论、以及「信号后移 1 日」这个具体判据都写进去了 ——
   提示词被谁删掉一段，测试立刻红。
2. **判定规则回归**：用一组确定性规则复刻提示词里写死的**硬判据**（成本模型缺失、
   用公告日前财务、当日收盘价成交、未做后移对照…），跑 4 个 fixture，
   断言 3 个已知错误样例判「不建议执行」、干净样例判「通过」。

注意：第 2 步是**预筛（pre-screen）**，不替代模型复核 —— 它的作用是保证「提示词里
承诺的硬规则」不会在重构中悄悄失效。真正的复核仍由 risk-reviewer 子智能体完成。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "risk_reviewer"
PROMPT_PATH = ROOT / "subagents" / "risk-reviewer.yaml"

VERDICT_PASS = "通过"
VERDICT_FIX = "补充措施后可执行"
VERDICT_REJECT = "不建议执行"


# ===========================================================================
# 1. 提示词覆盖度
# ===========================================================================
@pytest.fixture(scope="module")
def prompt_text() -> str:
    assert PROMPT_PATH.exists(), f"缺少 {PROMPT_PATH}"
    return PROMPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("dimension", [
    "前视偏差", "幸存者偏差", "过拟合", "样本内外", "绩效",
    "集中度", "流动性", "可成交性", "交易成本", "杠杆", "红线",
])
def test_prompt_covers_all_11_dimensions(prompt_text, dimension):
    assert dimension in prompt_text, f"risk-reviewer 提示词漏掉了复核维度：{dimension}"


@pytest.mark.parametrize("verdict", [VERDICT_PASS, VERDICT_FIX, VERDICT_REJECT])
def test_prompt_declares_three_verdict_levels(prompt_text, verdict):
    assert verdict in prompt_text, f"risk-reviewer 提示词未定义结论档：{verdict}"


def test_prompt_requires_lag_shift_test(prompt_text):
    assert "后移" in prompt_text and "1 个交易日" in prompt_text, \
        "risk-reviewer 提示词必须包含「信号整体后移 1 个交易日」的对照测试要求"


def test_prompt_hard_fails_on_missing_cost_model(prompt_text):
    assert "cost_model_json" in prompt_text and "不通过" in prompt_text, \
        "提示词必须写明：cost_model_json 缺失直接判不通过"


def test_prompt_forbids_relaxing_risk_controls(prompt_text):
    assert "杠杆" in prompt_text and "捷径" in prompt_text, \
        "提示词必须包含「不给放宽风控或加杠杆的捷径」的红线"


def test_prompt_is_readonly_and_has_budget(prompt_text):
    assert "disallowed_tools" in prompt_text
    assert '"write_file"' in prompt_text, "复核代理不得写库/写文件"
    assert '"task"' in prompt_text, "复核代理不得再委托子代理"
    assert "max_turns" in prompt_text and "timeout_seconds" in prompt_text


def test_subagent_fragment_will_not_duplicate_top_level_key():
    """risk-reviewer 必须是缩进片段 —— 直接追加第二个顶层 subagents: 会让 YAML 解析失败。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        assert not line.startswith("subagents:"), \
            "risk-reviewer.yaml 出现了顶层 subagents: 键，会与 machine-problems 的注入冲突"
    assert "\n    risk-reviewer:" in text, "risk-reviewer: 需以 4 空格缩进挂在 custom_agents 下"
    assert "# BEGIN quant-agent managed block" in text
    assert "# END quant-agent managed block" in text


# ===========================================================================
# 2. 判定规则回归（复刻提示词里的硬判据）
# ===========================================================================
def screen(record: dict) -> tuple[str, list[str]]:
    """把提示词里的硬判据写成确定性规则，返回 (结论, 命中的维度)。"""
    hits: list[str] = []
    hard_reject = False

    # 维度 9 交易成本：cost_model_json 缺失 → 直接不通过
    if not record.get("cost_model"):
        hits.append("交易成本")
        hard_reject = True

    # 维度 1 前视偏差：三选一命中即不通过
    factors = record.get("factors") or []
    uses_fundamentals_without_pub_date = any(
        f.get("lookback_source") == "fundamentals" and not f.get("uses_pub_date")
        for f in factors)
    execution = record.get("execution") or {}
    fills_same_day = not execution.get("fill_on_next_trading_day", True)
    mixed_adjust = not execution.get("adjust_mode_consistent", True)
    if uses_fundamentals_without_pub_date or fills_same_day or mixed_adjust:
        hits.append("前视偏差")
        hard_reject = True

    # 维度 1 对照测试：未做后移对照 → 降档；后移后由正转负 → 不通过
    lag = record.get("lag_shift_test") or {}
    lag_checked = bool(lag.get("checked"))
    if lag.get("turned_negative") is True:
        hits.append("前视偏差")
        hard_reject = True

    # 维度 2 幸存者偏差
    universe = record.get("universe") or {}
    survivorship_bad = (not universe.get("includes_delisted", True)) or \
                       (not universe.get("point_in_time", True))
    if survivorship_bad:
        hits.append("幸存者偏差")

    # 维度 4 样本内外：只有样本内 → 不通过
    sample = record.get("sample") or {}
    if not sample.get("has_out_of_sample"):
        hits.append("样本内外")
        hard_reject = True

    # 维度 3 过拟合：网格过大或邻域敏感断崖 → 降档
    over = record.get("overfitting") or {}
    overfit_bad = (int(over.get("param_grid_size") or 0) > 200) or \
                  (over.get("param_neighborhood_stable") is False)
    if overfit_bad:
        hits.append("过拟合")

    # 维度 6 集中度 / 维度 10 杠杆
    limits = record.get("limits") or {}
    conc = record.get("concentration") or {}
    if limits.get("max_position_pct") and conc.get("max_single_weight") and \
            conc["max_single_weight"] > limits["max_position_pct"] + 1e-9:
        hits.append("集中度")
    if (record.get("plan") or {}).get("leverage"):
        hits.append("杠杆")

    # 维度 7 流动性：拟下单金额占日均成交额比过高 → 降档
    liq = record.get("liquidity") or {}
    plan = record.get("plan") or {}
    if liq.get("min_avg_amount") and plan.get("total_order_amount"):
        if plan["total_order_amount"] / liq["min_avg_amount"] > 0.01:
            hits.append("流动性")

    # 维度 8 可成交性
    if not plan.get("tradability_checked", False) and plan.get("symbols"):
        pass  # 计划缺失可成交性核对时只提请注意，不单独降档（取数可核）

    if hard_reject:
        return VERDICT_REJECT, sorted(set(hits))
    if not lag_checked or survivorship_bad or overfit_bad or "流动性" in hits:
        if not lag_checked:
            hits.append("前视偏差")
        return VERDICT_FIX, sorted(set(hits))
    return VERDICT_PASS, sorted(set(hits))


def load_cases() -> list[dict]:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert files, f"{FIXTURE_DIR} 下没有 fixture"
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def test_fixture_set_is_3_bad_1_good():
    cases = load_cases()
    bad = [c for c in cases if c["expected_verdict"] != VERDICT_PASS]
    good = [c for c in cases if c["expected_verdict"] == VERDICT_PASS]
    assert len(bad) == 3 and len(good) == 1, \
        f"fixture 应为 3 错 1 对，实际 {len(bad)} 错 {len(good)} 对"


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["case_id"])
def test_screen_matches_expected_verdict(case):
    verdict, hits = screen(case["record"])
    assert verdict == case["expected_verdict"], (
        f"{case['case_id']} 期望「{case['expected_verdict']}」，实际「{verdict}」"
        f"（命中维度：{hits}）—— 说明判据太软，提示词需回炉")
    for dim in case.get("expected_dimensions") or []:
        assert dim in hits, f"{case['case_id']} 未命中预期维度：{dim}"


def test_screen_never_passes_the_bad_cases():
    """最关键的断言：三个已知错误样例一个都不能被判通过。"""
    bad_verdicts = [screen(c["record"])[0] for c in load_cases()
                    if c["expected_verdict"] != VERDICT_PASS]
    assert VERDICT_PASS not in bad_verdicts, \
        "存在错误样例被判「通过」—— 复核规则失效，必须先修提示词再谈上线"
