"""Layer 3: 数学决策 Hub + LLM 判断 + 分歧标记"""
from agent_layer.debate.models import FinalDecision
from common.logging import get_logger

logger = get_logger("agent.debate.decision_hub")


class DecisionHub:
    def __init__(self, provider_manager=None):
        self._pm = provider_manager

    def decide(self, fulcrum_verdict: str, factor_scores: dict,
               expert_reports=None, bull_args=None, bear_args=None) -> FinalDecision:
        # AG-COMPOSE (2026-09-07): 裁决官收全量意见 — 六专家 (score/key_points/
        # confidence/tier_used) + 多空论点 + fulcrum 仲裁 + 因子分, 终审一次成型。
        composite = sum(factor_scores.values()) / max(len(factor_scores), 1) if factor_scores else 0

        # 数学判断
        math_verdict, math_confidence, position_pct = self._math_judgment(composite)

        # 专家意见面 (供 LLM 终审; 空面时 LLM 腿退化为纯 fulcrum 模式)
        expert_lines = []
        for r in (expert_reports or []):
            try:
                tier = getattr(r, "tier_used", "rules")
                pts = "; ".join((r.key_points or [])[:2])
                expert_lines.append(f"{r.expert_name}[{tier}] score={r.score:+.2f} "
                                    f"conf={r.confidence:.2f} {pts}")
            except Exception:
                continue
        bull_text = "; ".join((bull_args or [])[:4])
        bear_text = "; ".join((bear_args or [])[:4])

        # LLM 终审 (cbcn 优先链; call_point=multi_agent_review 归 3000 预算桶)
        llm_verdict = None
        llm_reasoning = None
        if self._pm and hasattr(self._pm, 'has_available_llm') and self._pm.has_available_llm():
            try:
                if expert_lines:
                    prompt = (
                        f"多智能体会诊终审。仲裁观点: {fulcrum_verdict}\n"
                        f"综合因子评分: {composite:.4f}\n"
                        f"六方专家意见:\n" + "\n".join(expert_lines) + "\n"
                        f"多方论点: {bull_text or '无'}\n"
                        f"空方论点: {bear_text or '无'}\n"
                        "请综合以上意见给出终审结论, 首行必须是"
                        "【推荐】/【不推荐】/【观望】三者之一, "
                        "随后不超过120字说明理由。"
                    )
                    cp = "multi_agent_review"
                else:
                    prompt = (f"判断股票: fulcrum={fulcrum_verdict}, "
                              f"综合评分={composite:.2f}。输出: 推荐/不推荐/观望")
                    cp = "decision_hub"
                llm_result = self._pm.call(prompt, call_point=cp)
                if llm_result:
                    llm_verdict = ("不推荐" if "不推荐" in llm_result
                                   else ("推荐" if "推荐" in llm_result else "观望"))
                    llm_reasoning = str(llm_result).strip()[:500]
            except Exception:
                pass

        # 分歧标记
        divergences = []
        if llm_verdict and llm_verdict != math_verdict:
            divergences.append({
                "source_a": "LLM", "verdict_a": llm_verdict,
                "source_b": "DecisionHub", "verdict_b": math_verdict,
                "reason": f"LLM says {llm_verdict} but math says {math_verdict}",
            })
            logger.info(f"Divergence: LLM={llm_verdict}, Math={math_verdict}")

        # 如果分歧，采纳数学结果（保守优先）
        verdict = math_verdict
        confidence = math_confidence

        risk_flags = []
        requires_human = False
        if divergences:
            risk_flags.append("LLM与数学判断分歧")
        if composite < -0.5:
            risk_flags.append("因子评分过低")
        if position_pct > 50:
            risk_flags.append("仓位偏高")
            requires_human = True

        logger.info(f"DecisionHub: {verdict} (confidence={confidence:.2f}, position={position_pct:.0f}%)")
        reasoning = llm_reasoning or f"仲裁={fulcrum_verdict}, 综合评分={composite:.2f}"
        if divergences:
            reasoning = f"[数学判断保守采纳: {math_verdict}] " + reasoning
        return FinalDecision(
            verdict=verdict,
            confidence=confidence,
            position_pct=position_pct,
            reasoning=reasoning,
            risk_flags=risk_flags,
            requires_human=requires_human,
        )

    def _math_judgment(self, composite: float) -> tuple:
        if composite > 0.3:
            return "推荐", min(0.5 + abs(composite) * 0.3, 0.9), min(30 + composite * 40, 60)
        elif composite < -0.3:
            return "不推荐", min(0.5 + abs(composite) * 0.3, 0.9), 0
        else:
            return "观望", 0.5, 10
