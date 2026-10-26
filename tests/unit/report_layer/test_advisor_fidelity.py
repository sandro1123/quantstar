# -*- coding: utf-8 -*-
"""忠实性守卫三闸 TDD — 推荐逻辑贯通卡 模块1b (卡: RECOMMENDATION_COHERENCE_CARD_20260920)

判据（每条必须 AssertionError 非 ImportError）：
  闸1 结论一致：加工不得改 verdict/方向/置信档位
  闸2 依据封闭：加工产出的事实性断言/数字/因子名 ⊆ 原料字段集合（幻觉防线）
  闸3 可追溯：产出每句带 source 标注，来源 ∈ {reasoning, key_points, invalidation_conditions}
守卫失败处理：不静默——降级为展示原始 reasoning 原文 + 术语小注（诚实水位）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.fidelity import FidelityGuard, GuardVerdict
from report_layer.advisor.models import RecommendationArgument, AdvisorSentence


# ---------- 夹具 ----------

def make_argument(**overrides):
    """原料论证（对齐 agent_layer/debate/models.py 现状形状）"""
    base = dict(
        code="600000",
        verdict="buy",
        confidence=0.72,
        position_pct=0.15,
        reasoning="动量因子近20日强势，且估值处于近三年低位，综合判断可买入",
        key_points=["动量因子近20日强势", "估值处于近三年低位"],
        invalidation_conditions=["跌破10.5元止损", "量能萎缩至日均五成以下"],
        factors=["momentum_strength", "book_yield"],
        numbers=["20日", "三年", "10.5元", "五成"],
    )
    base.update(overrides)
    return RecommendationArgument(**base)


# ---------- 闸1 结论一致 ----------

class TestGate1VerdictIntegrity:
    def test_faithful_rewrite_passes(self):
        """纯润色（结论不变）→ 守卫放行"""
        arg = make_argument()
        out = [
            AdvisorSentence(text="该股动量因子近20日强势，且估值处于近三年低位，当前市场里属于值得关注的候选",
                            source="reasoning"),
        ]
        assert FidelityGuard(arg).check(out).ok is True

    def test_verdict_flip_rejected(self):
        """buy 被加工成 sell/avoid → 必须拒"""
        arg = make_argument()
        out = [AdvisorSentence(text="综合判断当前不宜买入，建议回避", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"

    def test_direction_flip_in_euphemism_rejected(self):
        """软化措辞但方向翻转（'买入'→'谨慎观望'）也算改结论"""
        arg = make_argument()
        out = [AdvisorSentence(text="当前或许可以观望为主", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"

    def test_confidence_band_upgrade_rejected(self):
        """低置信原料被加工成高置信表述 → 拒"""
        arg = make_argument(confidence=0.45)
        out = [AdvisorSentence(text="强烈建议买入", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"


# ---------- 闸2 依据封闭（幻觉防线） ----------

class TestGate2EvidenceClosure:
    def test_fabricated_fact_rejected(self):
        """凭空添加原料里没有的依据（'历史战绩显示…'）→ 必须拒"""
        arg = make_argument()
        out = [
            AdvisorSentence(text="该股动量强势；历史战绩显示该策略胜率八成", source="reasoning"),
        ]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "evidence"
        assert "历史战绩" in v.detail

    def test_fabricated_number_rejected(self):
        """编造原料没有的数字（目标价/胜率%）→ 拒"""
        arg = make_argument()
        out = [AdvisorSentence(text="该股动量强势，目标价上看15元", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "evidence"

    def test_fabricated_factor_name_rejected(self):
        """引用原料没有的因子名 → 拒"""
        arg = make_argument()
        out = [AdvisorSentence(text="该股换手率因子表现突出", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "evidence"

    def test_all_material_numbers_allowed(self):
        """只用原料里出现过的数字 → 放行"""
        arg = make_argument()
        out = [AdvisorSentence(text="该股近20日动量强势，跌破10.5元则止损", source="invalidation_conditions")]
        assert FidelityGuard(arg).check(out).ok is True

    def test_translated_term_backed_by_material(self):
        """术语翻译后的表述仍须锚回原料（动量强度→预测力属于翻译不算编造）"""
        arg = make_argument()
        out = [AdvisorSentence(text="该因子近20日的预测力表现强势", source="key_points")]
        assert FidelityGuard(arg).check(out).ok is True


# ---------- 闸3 可追溯 ----------

class TestGate3Traceability:
    def test_missing_source_rejected(self):
        """每句必须带 source"""
        arg = make_argument()
        out = [AdvisorSentence(text="该股动量强势", source=None)]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "trace"

    def test_unknown_source_rejected(self):
        """source 不在白名单三来源 → 拒"""
        arg = make_argument()
        out = [AdvisorSentence(text="该股动量强势", source="rumor")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "trace"


# ---------- 守卫失败降级（诚实水位） ----------

class TestDegradation:
    def test_guard_failure_returns_raw_reasoning(self):
        """守卫拒绝时降级路径 = 原始 reasoning 原文 + 术语小注，不是静默也不是编造"""
        arg = make_argument()
        out = [AdvisorSentence(text="历史战绩显示胜率八成", source="reasoning")]
        r = FidelityGuard(arg).degrade(out)
        assert r.degraded is True
        assert arg.reasoning in r.text
        assert "模拟盘研究性质" not in r.text  # 降级文本不含编造补充（免责句由渲染层统一加）

    def test_pass_path_not_degraded(self):
        arg = make_argument()
        out = [AdvisorSentence(text="该股动量强势", source="reasoning")]
        r = FidelityGuard(arg).degrade(out)
        assert r.degraded is False


# ---------- 模型形状 ----------

class TestModels:
    def test_argument_fields_align_with_debate_models(self):
        """原料形状须能从 FinalDecision/ExpertReport 字段构造（W1 schema 出后只换映射）"""
        arg = make_argument()
        assert arg.verdict == "buy"
        assert arg.invalidation_conditions

    def test_sentence_requires_source_field(self):
        s = AdvisorSentence(text="x", source="reasoning")
        assert s.source == "reasoning"
