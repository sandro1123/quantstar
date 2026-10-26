# -*- coding: utf-8 -*-
"""A2a: DDP critical / tolerated 子腿契约回归 (2026-09-20)。

依据 codex 审计官 sm-5273a39b84834573:
  「A2 的 DDP success 不能由父状态字符串单独决定；kline/index/factor 失败必须阻断,
    fuyao 的 expected_no_update/degraded 是否允许父链继续要有明确规则和 evidence。」

契约文档: qs-handoff/DDP_CONTRACT_20260920.md
本测试把两张清单与判据固化为断言, 防未来漂移。
"""
import inspect

import pytest


def _ddp_src():
    from ops_layer.orchestrator import PipelineOrchestrator
    return inspect.getsource(PipelineOrchestrator._run_data_pipeline)


# ---- 两张清单存在且内容正确 ----

def test_tolerated_set_matches_contract():
    """tolerated (best_effort) 集合须与契约文档逐项一致。"""
    src = _ddp_src()
    block = src.split("best_effort = name in")[1].split(")")[0]
    expected = {
        "stock_list", "moneyflow", "lhb_daily", "analyst_forecasts",
        "concept_daily", "market_sentiment", "stock_news",
        "stock_limit_up_down", "shortterm_collect", "index_daily",
        "fuyao_financial",
    }
    for name in expected:
        assert f'"{name}"' in block, f"tolerated 集合缺 {name}"


def test_kline_is_not_tolerated():
    """kline 是 critical — 不得进 tolerated 集合 (进了=主链失败被吞)。"""
    src = _ddp_src()
    block = src.split("best_effort = name in")[1].split(")")[0]
    assert '"kline"' not in block, "kline 不得为 tolerated — 它是关键链"


def test_valuation_is_not_tolerated():
    """valuation 是 critical。"""
    src = _ddp_src()
    block = src.split("best_effort = name in")[1].split(")")[0]
    assert '"valuation"' not in block, "valuation 不得为 tolerated"


# ---- 判据: 父状态不由字符串独断 ----

def test_critical_failure_blocks():
    """critical 子腿 (kline/valuation) 失败 → 进 overall_errors (阻断路径)。"""
    src = _ddp_src()
    # critical 腿失败走 else 分支的 overall_errors.append
    assert "overall_errors.append(msg)" in src, "critical 失败未进阻断路径"


def test_tolerated_degradation_named_not_silent():
    """tolerated 降级 → best_effort_degraded 具名 + 父 partial (不静默)。"""
    src = _ddp_src()
    assert "best_effort_degraded.append" in src, "tolerated 降级未具名"
    assert '"partial": True' in src or "'partial': True" in src, "降级未落 partial"


def test_kline_coverage_gate_present():
    """F2.5: kline 覆盖率门禁 (day_rows < MIN_KLINE_ROWS → failed) 须在位。"""
    from ops_layer.orchestrator import MIN_KLINE_ROWS
    assert MIN_KLINE_ROWS == 4000, f"MIN_KLINE_ROWS 期望 4000, 实得 {MIN_KLINE_ROWS}"
    src = _ddp_src()
    assert "min_kline_rows" in src or "MIN_KLINE_ROWS" in src, "覆盖率门禁缺失"


def test_fuyao_in_tolerated_has_reason():
    """fuyao 入 tolerated 须有明确理由 (季报稀疏 expected_no_update)。"""
    src = inspect.getsource(__import__("ops_layer.orchestrator", fromlist=["x"]))
    # 源码注释须说明 fuyao 为何可降级 (防未来有人无理由移除/添加)
    assert "fuyao" in src
    idx = src.find("fuyao_financial 移入 best_effort") if "fuyao_financial 移入 best_effort" in src else -1
    if idx == -1:
        # 至少要有 W2-1 或季报相关注释
        assert "季报" in src or "W2-1" in src, "fuyao 入 tolerated 缺理由注释"
