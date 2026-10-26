"""Layer 2: 多空对抗辩论"""
from agent_layer.debate.models import ExpertReport
from common.logging import get_logger

logger = get_logger("agent.debate.bull_bear")


class BullAgent:
    """强行看多 — 从专家报告提取所有看多论据"""

    def generate(self, reports: list[ExpertReport]) -> list[str]:
        args = []
        for r in reports:
            if r.score > 0:
                args.extend([f"[{r.expert_name}] {p}" for p in r.key_points])
        if not args:
            args = ["基本面稳健，估值合理"]
        logger.info(f"BullAgent: {len(args)} arguments")
        return args

    def rebut(self, bear_args: list[str], own_args: list[str] | None = None) -> list[str]:
        if own_args is None:
            return ["看多观点维持"]
        rebuttal = ["针对看空观点的质疑，核心逻辑依然成立"]
        rebuttal.extend(own_args[:3])
        return rebuttal


class BearAgent:
    """强行看空 — 从专家报告提取所有看空论据"""

    def generate(self, reports: list[ExpertReport]) -> list[str]:
        args = []
        for r in reports:
            if r.score < 0:
                args.extend([f"[{r.expert_name}] {p}" for p in r.key_points])
        if not args:
            args = ["估值偏高，动能不足"]
        logger.info(f"BearAgent: {len(args)} arguments")
        return args

    def rebut(self, bull_args: list[str], own_args: list[str] | None = None) -> list[str]:
        if own_args is None:
            return ["看空观点维持"]
        rebuttal = ["针对看多观点的质疑"]
        rebuttal.extend(own_args[:3])
        return rebuttal
