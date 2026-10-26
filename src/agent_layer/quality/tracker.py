"""质量追踪"""

from agent_layer._models import AccuracyReport
from agent_layer.core.output_spec import normalize_verdict
from agent_layer.quality.repository import AgentQualityRepository


class QualityTracker:
    def __init__(self, db=None):
        self.repo = AgentQualityRepository(db)
        self.repo.ensure_tables()

    # AG-03: 统一词表 — 中性判定=观望/警惕 (历史行经 normalize_verdict 读时归一化)
    NEUTRAL_VERDICTS = {"观望", "警惕"}

    def write_prediction(
        self,
        request_id: str,
        agent_name: str,
        code: str,
        date: str,
        verdict: str,
        confidence: float,
    ):
        self.repo.write_prediction(request_id, agent_name, code, date, verdict, confidence)

    def evaluate(self, date: str, klines: dict[str, float]):
        """用实际收益验证预测，计算命中率"""
        rows = self.repo.get_predictions_by_date(date)
        for row in rows:
            actual = klines.get(row["code"])
            if actual is None:
                continue
            # AG-03: 存量历史行读时归一化到统一词表
            verdict = normalize_verdict(row["verdict"])
            if verdict in self.NEUTRAL_VERDICTS:
                hit = None
            else:
                hit = (verdict == "推荐" and actual > 0) or (
                    verdict == "不推荐" and actual <= 0
                )
            self.repo.update_prediction_result(
                row["id"], actual, int(hit) if hit is not None else None
            )

    def get_accuracy(self, agent_name: str | None = None, period: str = "monthly") -> AccuracyReport:
        days = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}.get(period, 30)
        raw = self.repo.get_accuracy_stats(agent_name, days)
        return AccuracyReport(
            total=raw.get("total", 0),
            hits=raw.get("hits", 0),
            accuracy=raw.get("accuracy", 0.0),
        )
