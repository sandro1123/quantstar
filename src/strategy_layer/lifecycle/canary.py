"""CanaryRelease — 策略版本灰度发布

实现状态 (ST-04, 2026-08-18): **未启用** — 仅单元测试引用, 生产零调用。
启用条件: 流量切分地基建成后接线 (G33–G45 接线顺序末位)。
依据: 决策账本 S-01; 三态律之"排队"态。
"""
from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("strategy.canary")


@dataclass
class CanaryConfig:
    version: str
    traffic_percent: float = 10.0
    min_traffic: float = 10.0
    max_traffic: float = 100.0
    step: float = 20.0
    min_days: int = 3
    success_threshold: float = 0.6
    rollback_on_failure: bool = True


@dataclass
class CanaryStatus:
    version: str
    current_traffic: float
    phase: str  # "ramping_up" | "stable" | "rolled_back" | "full_release"
    days_active: int
    metric_scores: dict[str, float] = field(default_factory=dict)


class CanaryRelease:
    def __init__(self):
        self._releases: dict[str, CanaryConfig] = {}
        self._statuses: dict[str, CanaryStatus] = {}

    def register(self, version: str, config: CanaryConfig | None = None) -> CanaryStatus:
        cfg = config or CanaryConfig(version=version)
        self._releases[version] = cfg
        status = CanaryStatus(version=version, current_traffic=cfg.min_traffic, phase="ramping_up", days_active=0)
        self._statuses[version] = status
        logger.info(f"Canary: {version} registered at {cfg.min_traffic}% traffic")
        return status

    def evaluate(self, version: str, metrics: dict[str, float]) -> CanaryStatus:
        status = self._statuses.get(version)
        if not status:
            raise ValueError(f"Unknown version: {version}")
        cfg = self._releases[version]
        status.days_active += 1
        status.metric_scores = metrics
        avg_score = sum(metrics.values()) / len(metrics) if metrics else 0
        if avg_score < cfg.success_threshold and cfg.rollback_on_failure:
            status.phase = "rolled_back"
            status.current_traffic = 0.0
            logger.warning(f"Canary: {version} rolled back (score={avg_score:.2f})")
            return status
        if status.days_active >= cfg.min_days and status.current_traffic < cfg.max_traffic:
            status.current_traffic = min(status.current_traffic + cfg.step, cfg.max_traffic)
            if status.current_traffic >= cfg.max_traffic:
                status.phase = "full_release"
                logger.info(f"Canary: {version} promoted to full release")
            else:
                status.phase = "ramping_up"
                logger.info(f"Canary: {version} ramped to {status.current_traffic}%")
        else:
            status.phase = "stable"
        return status

    def get_status(self, version: str) -> CanaryStatus | None:
        return self._statuses.get(version)

    def rollback(self, version: str) -> bool:
        status = self._statuses.get(version)
        if not status:
            return False
        status.phase = "rolled_back"
        status.current_traffic = 0.0
        logger.warning(f"Canary: {version} manually rolled back")
        return True
