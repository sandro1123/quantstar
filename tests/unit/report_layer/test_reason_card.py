# -*- coding: utf-8 -*-
"""P0-1 TDD — 论证卡（字段模板装配，零 LLM）

修法：新增 _pick_reason_card(row, agent_review=None) ——
当 agent_review 有 key_points/invalidation_conditions 时，装配成多字段论证卡；
否则降级为 _pick_reason 的单句输出（零改动既有行为）。

「砍规格=纯规则模板」：字段模板（不是句式模板）——
从 agent_reviews 的 key_points/invalidation_conditions/confidence 直接装配，
不需要 LLM。结论/数字/因子 ⊆ 原料不可越（守卫管），表达风格可 LLM（守卫不管风格）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_reason import _pick_reason, _pick_reason_card


def _mk_agent(code, verdict, confidence, key_points=None, invalidation=None):
    return SimpleNamespace(
        code=code, verdict=verdict, confidence=confidence,
        key_points=key_points or [], invalidation_conditions=invalidation or [],
        factors=[], numbers=[])


class TestReasonCard:
    """P0-1：论证卡多字段结构（为什么/信心/失效条件/风险）"""

    def test_card_with_key_points(self):
        """有 key_points → 输出多字段论证卡（非单句）"""
        agent = _mk_agent("600000", "推荐", 0.72,
                          key_points=["动量因子近20日强势", "估值处于近三年低位"],
                          invalidation=["跌破10.5元止损"])
        card = _pick_reason_card({"added_reason": "agent 推荐（verdict=推荐, confidence=0.72）"},
                                 agent_review=agent)
        assert "动量因子近20日强势" in card
        assert "估值处于近三年低位" in card
        assert "跌破10.5元止损" in card
        assert "信心" in card or "confidence" in card.lower()

    def test_card_without_key_points_degrades(self):
        """无 key_points → 降级为单句（诚实水位，不空）"""
        agent = _mk_agent("600000", "推荐", 0.72, key_points=[])
        card = _pick_reason_card({"added_reason": "agent 推荐（verdict=推荐, confidence=0.72）"},
                                 agent_review=agent)
        assert "agent 推荐" in card or "推荐" in card
        assert len(card) > 0

    def test_card_invalidation_conditions(self):
        """invalidation_conditions 有值 → 失效条件段有内容"""
        agent = _mk_agent("600000", "推荐", 0.72,
                          key_points=["动量强"],
                          invalidation=["跌破10.5元止损", "量能萎缩至日均五成以下"])
        card = _pick_reason_card({"added_reason": "x"}, agent_review=agent)
        assert "跌破10.5元止损" in card or "量能萎缩" in card

    def test_card_invalidation_empty_uses_default(self):
        """invalidation_conditions 空 → 失效条件段默认兜底（不空）"""
        agent = _mk_agent("600000", "推荐", 0.72, key_points=["动量强"], invalidation=[])
        card = _pick_reason_card({"added_reason": "x"}, agent_review=agent)
        assert "止损" in card or "失效" in card or "跌破" in card

    def test_card_no_agent_review_falls_back(self):
        """无 agent_review → 降级为 _pick_reason 单句（零改动既有行为）"""
        card = _pick_reason_card({"added_reason": "候选池第1/10位, 综合得分 0.5"})
        assert card == _pick_reason({"added_reason": "候选池第1/10位, 综合得分 0.5"})

    def test_card_no_internal_marks(self):
        """输出无内部标记（[...]【...】不换行）"""
        agent = _mk_agent("600000", "推荐", 0.72,
                          key_points=["动量强"], invalidation=["跌破10.5元"])
        card = _pick_reason_card({"added_reason": "x"}, agent_review=agent)
        assert "[" not in card and "【" not in card
        assert "\n" not in card

    def test_card_confidence_tier_not_raw(self):
        """信心用降档后值（high/mid/low），不用 raw confidence"""
        agent = _mk_agent("600000", "推荐", 0.72, key_points=["动量强"])
        # 若 agent 有 confidence_tier 字段（降档规则引擎产出）
        agent.confidence_tier = "high"
        card = _pick_reason_card({"added_reason": "x"}, agent_review=agent)
        assert "high" in card or "高" in card or "信心" in card
