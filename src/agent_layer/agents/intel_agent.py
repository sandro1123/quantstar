"""IntelAgent — 资金流分析（纯规则，无 LLM）"""
from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent


class IntelAgent(BaseAgent):
    name = "intel_agent"

    def analyze(self, data: dict) -> AgentResult:
        turnover = float(data.get("turnover_ratio", 0) or 0)
        volume_change = float(data.get("volume_change", 0) or 0)
        main_net = float(data.get("main_net_inflow", 0) or 0)

        score = 0.0
        if turnover > 5:
            score += 0.3
        elif turnover < 1:
            score -= 0.2
        if volume_change > 0.5:
            score += 0.2
        elif volume_change < -0.3:
            score -= 0.2
        if main_net > 0:
            score += 0.2
        elif main_net < 0:
            score -= 0.2

        return AgentResult(
            verdict="推荐" if score > 0.3 else ("观望" if score > -0.2 else "不推荐"),
            confidence=min(abs(score) + 0.3, 0.9),
            reasoning=f"资金流分析: 换手率={turnover}%, 量比={volume_change:.2f}, 主力净流入={main_net:.0f}",
            score=score,
        )
