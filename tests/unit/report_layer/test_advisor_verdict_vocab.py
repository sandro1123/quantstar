# -*- coding: utf-8 -*-
"""审计复核 P0① 修复判据 — 闸1 verdict 词表与 AG-03 同源

审计实测（QS_1B_GUARD_REVIEW_20260921.md）：
  verdict=「推荐」（AG-03 中文词表，decision_hub 实产）+ 文本「不宜买入回避」
  → 旧映射把「推荐」误判 -1，与文本 -1 相同 → 闸1 放行 → **真翻转全闸通过**。
修法：闸1 映射与 AG-03 同源（推荐/买入→+1，不推荐/卖出/规避→-1，观望/警惕/持有→0）。
source.py 的 _VERDICT_MAP 是 agent_memory 回落路径专用，正交不动。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.fidelity import FidelityGuard, _direction_of_verdict
from report_layer.advisor.models import RecommendationArgument, AdvisorSentence


def make_arg(**kw):
    base = dict(code="600000", verdict="推荐", confidence=0.72, position_pct=0.15,
                reasoning="动量因子近20日强势", key_points=[], invalidation_conditions=[],
                factors=[], numbers=[])
    base.update(kw)
    return RecommendationArgument(**base)


class TestVerdictVocabAg03:
    """AG-03 中文词表 → 方向（与 agent_layer 同源）"""

    @pytest.mark.parametrize("v", ["推荐", "买入"])
    def test_positive_vocab(self, v):
        assert _direction_of_verdict(v) == 1

    @pytest.mark.parametrize("v", ["不推荐", "卖出", "规避"])
    def test_negative_vocab(self, v):
        assert _direction_of_verdict(v) == -1

    @pytest.mark.parametrize("v", ["观望", "警惕", "持有"])
    def test_neutral_vocab(self, v):
        assert _direction_of_verdict(v) == 0

    @pytest.mark.parametrize("v,want", [("buy", 1), ("hold", 0), ("avoid", -1)])
    def test_english_path_unchanged(self, v, want):
        """英文路径行为不变（15 测回归锚）"""
        assert _direction_of_verdict(v) == want

    def test_unknown_still_conservative(self):
        assert _direction_of_verdict("天知道") == -1
        assert _direction_of_verdict(None) == -1


class TestAuditScenarioBlocked:
    """审计官实测场景：必须从「放行」变为「拒」"""

    def test_recommend_with_negative_text_rejected(self):
        """verdict=推荐 + 文本「不宜买入回避」→ 闸1 拒（审计 P0① 原案）"""
        arg = make_arg(verdict="推荐")
        out = [AdvisorSentence(text="综合判断当前不宜买入，建议回避", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"

    def test_not_recommend_with_positive_text_rejected(self):
        """反向：verdict=不推荐 + 文本「建议买入」→ 拒（双向对称）"""
        arg = make_arg(verdict="不推荐")
        out = [AdvisorSentence(text="该股动量强势，建议买入", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"

    def test_hold_with_positive_text_rejected(self):
        """同族第4处：观望(0) 被加工成买入(+1) 也是方向翻转（原实现 want_dir!=0 短路放行）"""
        arg = make_arg(verdict="观望")
        out = [AdvisorSentence(text="该股动量强势，建议买入", source="reasoning")]
        v = FidelityGuard(arg).check(out)
        assert v.ok is False
        assert v.gate == "conclusion"

    def test_hold_with_neutral_text_passes(self):
        """观望 + 中性文本 → 放行（不误伤）"""
        arg = make_arg(verdict="观望")
        out = [AdvisorSentence(text="该股近期震荡整理，成交量温和", source="reasoning")]
        assert FidelityGuard(arg).check(out).ok is True

    def test_recommend_with_positive_text_passes(self):
        """推荐 + 正向文本 → 放行（正路不被误伤）"""
        arg = make_arg(verdict="推荐")
        out = [AdvisorSentence(text="该股动量强势，值得关注", source="reasoning")]
        assert FidelityGuard(arg).check(out).ok is True
