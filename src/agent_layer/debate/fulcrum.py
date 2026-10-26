"""Layer 3: 中立仲裁者"""
from agent_layer.debate.models import ExpertReport
from common.logging import get_logger

logger = get_logger("agent.debate.fulcrum")


class Fulcrum:
    """中立仲裁者 — 读专家报告 + 多空论据 → 定性建议"""

    def analyze(self, expert_reports: list[ExpertReport], bull_args: list[str], bear_args: list[str]) -> str:
        bull_count = len(bull_args)
        bear_count = len(bear_args)

        bull_score = sum(1 for r in expert_reports if r.score > 0)
        bear_score = sum(1 for r in expert_reports if r.score < 0)

        if bull_score > bear_score:
            consensus = "偏多"
            reasoning = f"看多论据({bull_count}条)多于看空({bear_count}条)，专家偏多"
        elif bear_score > bull_score:
            consensus = "偏空"
            reasoning = f"看空论据({bear_count}条)多于看多({bull_count}条)，专家偏空"
        else:
            consensus = "中性"
            reasoning = "多空论据均衡，暂无明确方向"

        logger.info(f"Fulcrum: {consensus}")
        return f"{consensus}: {reasoning}"
