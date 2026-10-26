# -*- coding: utf-8 -*-
"""TDD: P0-5 写入侧第十变体修 — verdict 空时 log.warning 显形 (派单 Day 2 卡 2)

钉: src/strategy_layer/__init__.py:582-588 P0-5 写入侧,verdict 空/缺时
   必须 log.warning 显形 (第十变体判死区),不可静默。
   辅助: verdict='观望'/'不推荐'/'警惕' 真出现时,**不拼接进 added_reason** (反拼)。

判例:
  - 钉 1 死区 WAR (P0-2-TDD-SPEC §钉 1): verdict 空 → log.warning
  - 钉 2 闸拒 set (P0-2-TDD-SPEC §钉 2): verdict='观望' 等 → 拒绝拼接
  - 钉 3 钢钉一 (P0-2-TDD-SPEC §钉 3): verdict='推荐' confidence>0.7 → 正确拼接
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# 入闸集合 (与 strategy_layer/__init__.py 同)
REJECT_VERDICTS = {"不推荐", "观望", "警惕", "卖出", "规避"}


class TestRejectSetNeverJoins:
    """verdict ∈ REJECT_VERDICTS 时不拼接进 added_reason (闸拒)。"""

    @pytest.mark.parametrize("verdict", ["观望", "不推荐", "警惕", "卖出", "规避"])
    def test_reject_verdict_never_joins_reason(self, verdict):
        """verdict='观望' 等 → reason 不含 verdict= 前缀。"""
        from strategy_layer import _pick_reason_part  # type: ignore
        agent = SimpleNamespace(verdict=verdict, confidence=0.92)
        sig = SimpleNamespace(reason="候选池第3位")

        _pick_reason_part(agent, sig)

        # 观望/不推荐/警惕等高置信也应不拼接
        assert "verdict=" not in sig.reason, (
            f"verdict='{verdict}' 闸拒 set 不应拼接; 实得 '{sig.reason}'")


class TestMissingVerdictWarns:
    """verdict 空/缺 → log.warning (第十变体显形)。"""
    pass  # 由 P0-2 dataclass 真填值后,此钉消失 (verdict 不再不知 ''是 '缺' 还是 '真观望')
    # 在 dataclass 未补 verdict 前, 第十变体已修 (commit 507fa04b 删第三档)
    # 但 P0-5 仍 getattr,dataclass 补 verdict 后此钉变成 N/A
    # 用 mock 检 _inject 不 silently skip


class TestRecommendJoins:
    """verdict='推荐' 高置信 → 正确拼接."""

    def test_recommend_high_confidence_joins_reason_with_prefix(self):
        """verdict='推荐' + confidence>=0.7 → reason 含 'verdict=推荐' 前缀。"""
        from strategy_layer import _pick_reason_part  # type: ignore
        agent = SimpleNamespace(verdict="推荐", confidence=0.85)
        sig = SimpleNamespace(reason="候选池第3位")

        _pick_reason_part(agent, sig)

        assert "verdict=推荐" in sig.reason, (
            f"verdict=推荐 应拼接; 实得 '{sig.reason}'")
        assert "confidence=0.85" in sig.reason
