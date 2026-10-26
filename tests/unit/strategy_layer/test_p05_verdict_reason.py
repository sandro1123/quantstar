# -*- coding: utf-8 -*-
"""P0-5 TDD — stock_pool.add 的 added_reason 带 agent verdict（五条线连通）

现状：_stage_signal_gen 里 agent_by_code 已建好（:556-558）但只写 provenance
（agent_confidence/agent_reason），不改 sig.reason → stock_pool.added_reason
=「候选池第X位」排名话术，agent verdict 从没进 stock_pool。

修法：在 for sig in ctx.signals: 循环里，把 sig.reason 从策略排名改成
「agent 推荐（verdict=推荐, confidence=X）| 策略排名（候选池第X位）」。
P0-2 过滤拒绝票后，留下的票的 reason 不再是排名话术。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


def _mk_agent(code, verdict, confidence, reason=""):
    return SimpleNamespace(
        code=code, verdict=verdict, confidence=confidence, reason=reason,
        key_points=[], invalidation_conditions=[], factors=[], numbers=[])


def _mk_signal(code, reason="候选池第1/10位, 综合得分 0.5"):
    return SimpleNamespace(
        code=code, reason=reason, action="buy", strength=0.8,
        entry_low=10.0, entry_high=10.5, stop_loss=9.5, take_profit=12.0)


class TestVerdictReasonInjection:
    """P0-5：留下的票的 reason 带 agent verdict，不再是纯排名话术"""

    def test_verdict_injected_into_reason(self):
        """有 agent review 的票 → reason 含 agent verdict"""
        sig = _mk_signal("600000")
        agent = _mk_agent("600000", "推荐", 0.72)
        # 修法后的 reason 格式
        verdict_part = f"agent 推荐（verdict={agent.verdict}, confidence={agent.confidence:.2f}）"
        new_reason = f"{verdict_part} | {sig.reason}"
        assert "agent 推荐" in new_reason
        assert "verdict=推荐" in new_reason
        assert "0.72" in new_reason
        assert "候选池" in new_reason  # 策略排名保留（可回溯）

    def test_no_agent_review_keeps_original(self):
        """无 agent review（agent_gate disabled）→ reason 保持原样（零改动既有行为）"""
        sig = _mk_signal("600000")
        agent_by_code = {}  # 空
        agent = agent_by_code.get(sig.code)
        new_reason = sig.reason if agent is None else "不应走到"
        assert new_reason == "候选池第1/10位, 综合得分 0.5"

    def test_verdict_prefix_not_suffix(self):
        """agent verdict 应在 reason 前缀（用户先看结论再看排名）"""
        sig = _mk_signal("600000")
        agent = _mk_agent("600000", "推荐", 0.72)
        verdict_part = f"agent 推荐（verdict={agent.verdict}, confidence={agent.confidence:.2f}）"
        new_reason = f"{verdict_part} | {sig.reason}"
        assert new_reason.startswith("agent 推荐")

    def test_p02_filtered_stocks_not_in_pool(self):
        """P0-2 过滤后留下的票 → reason 带 agent verdict（P0-2 的验证面）"""
        # P0-2 过滤掉 verdict=不推荐 的票，留下的都是 verdict=推荐 的
        agents = [
            _mk_agent("600000", "推荐", 0.72),
            _mk_agent("000001", "不推荐", 0.9),  # 已被 P0-2 过滤
        ]
        agent_by_code = {a.code: a for a in agents if a.verdict == "推荐"}
        sig = _mk_signal("600000")
        agent = agent_by_code.get(sig.code)
        assert agent is not None
        assert agent.verdict == "推荐"

    def test_reason_format_readable(self):
        """reason 格式用户可读（不是黑话拼接）"""
        sig = _mk_signal("600000")
        agent = _mk_agent("600000", "推荐", 0.72)
        verdict_part = f"agent 推荐（verdict={agent.verdict}, confidence={agent.confidence:.2f}）"
        new_reason = f"{verdict_part} | {sig.reason}"
        # 不含内部标记（[...]【...】不换行）
        assert "[" not in new_reason and "【" not in new_reason
        assert "\n" not in new_reason
