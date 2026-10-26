# -*- coding: utf-8 -*-
"""REC-M4-followup (2026-09-24): 信心降档映射规则引擎。

统筹设计定稿 (三裁):
a) 每腿一档封顶 + 多腿取最低, 单次最多降两档 (高→低需两腿缺);
b) rules_hash 格式定死: `{人话原因}｜rules:{hash前8位}` (审计可正则解析);
c) 红线: **宁可降档不可假装高** —— 任一腿判据**取不到数据** (非「腿健康」
   而是「查不到」) → 按腿缺处理降档, 不得默认健康 (诚实水位延伸)。

六条规则 (初版定稿):
R1 LLM 辩论腿缺 (tier_used=="rules" / 无 rationale_id) → 降一档
R2 K线/估值腿缺 (当日 klines 覆盖 <85% 或当日行缺失) → 降一档
R3 资金流腿缺 (当日 stock_moneyflow 无该 code 行) → 降一档
R4 news_intel 腿缺 (当日 stock_news 无该 code 行) → 降一档
R5 多腿缺 (>=2) → 直接低档 (取最低, 不叠加)
R6 全腿齐 + 高置信 → 高

留痕: 降档命中写 rationale.claims {kind:"rule", ref:"rule_id=...", value:"high→mid"}
—— 与出口硬闸同构 (可 JOIN rule_id); rules_hash 进 confidence_reason 尾部。
"""
import json
import sqlite3

import pytest

from common.database import Database


# 判例#8
def test_module_exists():
    try:
        import execution_layer.tracking.confidence_rules  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"confidence_rules 未建: {e}") from e


def _db():
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    ensure_schema(db)
    return db, db.get_connection()


# ---- 六条规则各触发正确降档 ----

def test_r1_llm_leg_missing_downgrades_one_tier():
    """R1: LLM 辩论腿缺 (tier_used=rules) → 高→中。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(
        base_confidence=0.8,
        leg_status={"llm_debate": "missing"},  # 其余腿健康
    )
    assert result["tier"] == "mid", f"高置信+LLM腿缺应降一档=中, 实得 {result['tier']}"
    assert any(r["rule_id"] == "R1" for r in result["hits"]), "命中规则须留痕 R1"


def test_r2_kline_leg_missing_downgrades():
    """R2: 当日 K线行缺失 → 降一档。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"kline_valuation": "missing"})
    assert result["tier"] == "mid"


def test_r3_moneyflow_leg_missing():
    """R3: 资金流腿缺 → 降一档。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"moneyflow": "missing"})
    assert result["tier"] == "mid"


def test_r5_two_legs_missing_direct_low():
    """R5: 两腿缺 → 直接低 (取最低不叠加, 统筹裁 a)。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.9, {"llm_debate": "missing", "moneyflow": "missing"})
    assert result["tier"] == "low", "两腿缺必须直接低档"
    assert result["tier"] == "low" and not result["tier"] == "unknown"


def test_r6_all_healthy_high_confidence_stays_high():
    """R6: 全腿齐 + 高置信 → 高 (不误伤)。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"llm_debate": "ok", "kline_valuation": "ok",
                               "moneyflow": "ok", "news_intel": "ok"})
    assert result["tier"] == "high", "全腿齐不降档"
    assert result["hits"] == [], "无降档即无命中留痕"


# ---- 红线: 取不到数据按腿缺降档 ----

def test_red_line_judge_unavailable_counts_as_missing():
    """红线: 判据取不到数据 (status="judge_unavailable") 按腿缺降档, 不默认健康。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"llm_debate": "judge_unavailable"})
    assert result["tier"] == "mid", \
        f"判据查不到必须按腿缺降档 (宁可降档不可假装高), 实得 {result['tier']}"
    assert any(r["rule_id"] == "R1" for r in result["hits"])


def test_red_line_unknown_status_counts_as_missing():
    """红线: 未知状态值 (非 ok 的任何值) 一律按腿缺 — 不许静默当健康。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"kline_valuation": "some_new_status_nobody_defined"})
    assert result["tier"] == "mid", \
        "未知状态必须降档 — 白名单外状态默认健康 = 假装高 (红线)"
    # 显式健康值才放行
    result2 = apply_rules(0.8, {"kline_valuation": "ok"})
    assert result2["tier"] == "high"


# ---- rules_hash 可追溯 ----

def test_rules_hash_in_reason_suffix():
    """格式定死: `{人话原因}｜rules:{hash前8位}` (统筹裁 b)。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"llm_debate": "missing"})
    assert "｜rules:" in result["confidence_reason"], \
        f"reason 须带 rules_hash 后缀, 实得: {result['confidence_reason']}"
    # hash 前 8 位
    suffix = result["confidence_reason"].split("｜rules:")[1]
    assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix), \
        f"hash 须 8 位 hex, 实得: {suffix}"
    # 人话原因在前段 (hash 不污染可读性)
    head = result["confidence_reason"].split("｜rules:")[0]
    assert "降档" in head or "信心" in head, f"人话段缺失: {head}"


def test_rules_hash_deterministic_and_sensitive_to_rules():
    """hash 对规则集确定性 (同规则集同 hash), 规则变了 hash 变 (可审计)。"""
    from execution_layer.tracking.confidence_rules import (
        apply_rules, _rules_hash, RULES)
    h1 = _rules_hash(RULES)
    h2 = _rules_hash(RULES)
    assert h1 == h2, "同规则集 hash 须确定"
    assert len(h1) >= 8
    modified = [dict(r) for r in RULES]
    modified.append({"id": "R9", "trigger": "x", "effect": "low"})
    assert _rules_hash(modified) != h1, "规则集变更 hash 必须变 (审计锚)"


def test_hit_record_claim_shape():
    """命中留痕 shape: {kind:"rule", ref:"rule_id=..", value:"high→mid"} (硬闸同构)。"""
    from execution_layer.tracking.confidence_rules import apply_rules
    result = apply_rules(0.8, {"llm_debate": "missing"})
    r1 = next(r for r in result["hits"] if r["rule_id"] == "R1")
    assert r1["claim"]["source"]["kind"] == "rule"
    assert r1["claim"]["source"]["ref"] == "rule_id=R1"
    assert "high" in r1["claim"]["source"]["value"], \
        f"降档效果须含源档→目标档: {r1['claim']['source']['value']}"
