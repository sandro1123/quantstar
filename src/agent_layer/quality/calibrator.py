"""置信度校准"""

from agent_layer.quality.repository import AgentQualityRepository
from common.logging import get_logger

logger = get_logger("agent.quality.calibrator")


class Calibrator:
    def __init__(self, database=None):
        self.repo = AgentQualityRepository(database)
        self._warned_no_data = False

    def get_accuracy(self, agent_name: str | None = None, days: int = 30) -> float | None:
        """历史命中率; 无评估数据或查询异常返回 None, 不再以 0.5 冒充 (AG-15 段1)."""
        try:
            total, hits = self.repo.get_accuracy_sum(agent_name, days)
            if total <= 0:
                return None
            return hits / total
        except Exception:
            logger.warning("get_accuracy query failed; calibration bypassed", exc_info=True)
            return None

    def calibrate(self, agent_name: str, raw_confidence: float) -> float:
        if not self.repo.db:
            return raw_confidence
        accuracy = self.get_accuracy(agent_name)
        if accuracy is None:
            # 无评估数据 → 透传原置信度, 不施加虚构折扣 (铁律#12: 可观测不可静默)
            if not self._warned_no_data:
                logger.warning(
                    "calibrator: no evaluation data (hit all NULL), calibration bypassed "
                    "for agent=%s — raw confidence passed through",
                    agent_name,
                )
                self._warned_no_data = True
            return raw_confidence
        calibrated = raw_confidence * (0.3 + 0.7 * accuracy)
        return round(min(0.95, max(0.05, calibrated)), 3)
