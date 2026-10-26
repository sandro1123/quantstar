"""StockAgent — 基本面/均线分析（纯规则，无 LLM）"""
from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent


class StockAgent(BaseAgent):
    name = "stock_agent"

    def analyze(self, data: dict) -> AgentResult:
        pe = float(data.get("pe", 0) or 0)
        pb = float(data.get("pb", 0) or 0)
        roe = float(data.get("roe", 0) or 0)
        score = 0.0
        if 0 < pe < 50:
            score += 0.2
        if 0 < pb < 5:
            score += 0.2
        if roe > 10:
            score += 0.3
        elif roe > 5:
            score += 0.1
        return AgentResult(
            verdict="推荐" if score > 0.4 else ("观望" if score > 0 else "不推荐"),
            confidence=min(score + 0.3, 0.9),
            reasoning=f"基本面分析: PE={pe}, PB={pb}, ROE={roe}%",
            score=score,
        )
