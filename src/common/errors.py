"""统一错误模型"""

from dataclasses import dataclass
from enum import Enum


class ErrorSeverity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class QuantStarError(Exception):
    layer: str
    severity: ErrorSeverity
    trace_id: str
    recoverable: bool
    suggestion: str | None = None

    def __str__(self) -> str:
        return (
            f"[{self.severity.value.upper()}] {self.layer}: "
            f"{self.args[0] if self.args else '未知错误'} "
            f"(trace_id={self.trace_id}, recoverable={self.recoverable})"
        )
