"""Provider Pool — 注册 + 配额 + 健康度"""
from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("agent.llm.pool")


@dataclass
class ProviderHealth:
    name: str
    score: float = 1.0
    total_calls: int = 0
    success_calls: int = 0
    recent_latencies: list = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_calls / self.total_calls if self.total_calls > 0 else 1.0

    def record_success(self, latency_ms: int):
        self.total_calls += 1
        self.success_calls += 1
        self.recent_latencies.append(latency_ms)
        if len(self.recent_latencies) > 100:
            self.recent_latencies.pop(0)
        self._update_score()

    def record_failure(self):
        self.total_calls += 1
        self._update_score()

    def _update_score(self):
        if self.total_calls < 5:
            self.score = 1.0
            return
        sr = self.success_rate
        avg_lat = sum(self.recent_latencies[-10:]) / max(len(self.recent_latencies[-10:]), 1)
        lat_score = max(0, 1 - avg_lat / 10000)
        self.score = sr * 0.7 + lat_score * 0.3


class ProviderPool:
    def __init__(self):
        self._health: dict[str, ProviderHealth] = {}
        self._quotas: dict[str, int] = {}
        self._usage: dict[str, int] = {}

    def register(self, name: str, quota: int = 1000):
        self._health[name] = ProviderHealth(name=name)
        self._quotas[name] = quota
        self._usage[name] = 0

    def get_available(self, min_score: float = 0.3) -> list[str]:
        available = []
        for name, health in self._health.items():
            if health.score >= min_score and self._usage.get(name, 0) < self._quotas.get(name, 9999):
                available.append(name)
        return sorted(available, key=lambda n: self._health[n].score, reverse=True)

    def record_success(self, name: str, latency_ms: int):
        if name in self._health:
            self._health[name].record_success(latency_ms)
        self._usage[name] = self._usage.get(name, 0) + 1

    def record_failure(self, name: str):
        if name in self._health:
            self._health[name].record_failure()

    def health_summary(self) -> dict:
        return {
            name: {"score": h.score, "success_rate": h.success_rate, "usage": self._usage.get(name, 0)}
            for name, h in self._health.items()
        }

    def total_remaining(self) -> int:
        total = 0
        for name in self._health:
            total += self._quotas.get(name, 0) - self._usage.get(name, 0)
        return total
