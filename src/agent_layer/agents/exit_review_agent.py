"""退出复盘 Agent — 纯规则 (AG-03: verdict 统一词表)

语义 (供 EX-03 消费): 推荐=退出合理(参考放行), 不推荐=退出存疑,
警惕=退出依据缺失。Agent 复核是参考信息, 不是 6 层引擎的闸门。
"""
from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent


class ExitReviewAgent(BaseAgent):
    name = "exit_review"

    def analyze(self, input_data: dict) -> AgentResult:
        exit_reason = input_data.get("exit_reason", "")
        hold_days = input_data.get("hold_days", 0)
        pnl_pct = input_data.get("pnl_pct", 0.0)
        risk_flags: list[str] = []

        if not exit_reason:
            risk_flags.append("missing_exit_reason")

        if hold_days < 1:
            risk_flags.append(f"hold_days:{hold_days} < 1")

        # 依据缺失优先 → 警惕 (参考信息, 不阻断)
        if not exit_reason:
            verdict = "警惕"
        elif isinstance(pnl_pct, (int, float)) and pnl_pct > 0:
            verdict = "推荐"
        elif isinstance(pnl_pct, (int, float)) and pnl_pct < 0:
            verdict = "不推荐"
        else:
            verdict = "观望"

        if risk_flags:
            reasoning = "; ".join(risk_flags)
        elif isinstance(pnl_pct, (int, float)):
            reasoning = f"收益 {pnl_pct:.1f}%，退出评估完成"
        else:
            reasoning = "退出评估完成"
        confidence = 0.8 if not risk_flags else 0.5

        return AgentResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            risk_flags=risk_flags,
        )
