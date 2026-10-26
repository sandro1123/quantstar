"""ops_layer/core/models.py — Ops Layer 数据模型"""

from dataclasses import dataclass, field


@dataclass
class HandlerResult:
    success: bool = True
    records_written: int = 0
    errors: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def final_status(self) -> str:
        """DL-43 三态: "success" | "partial" | "failed"。

        partial 由 metadata["partial"] 显式标记 (数据已落库但降级)。此前
        partial 被 success=True 折叠成 "success" 静默入库 —— 正是 DL-06
        "报 success 但表无数据/数据残缺" 的假成功病灶。本属性是 run_pipeline
        落 pipeline_runs.status 的唯一状态来源, 保证三态可区分。
        """
        if not self.success:
            return "failed"
        if self.metadata.get("partial"):
            return "partial"
        return "success"


@dataclass
class HealthCheck:
    name: str
    status: str               # "pass" | "warn" | "fail"
    detail: str
    metric: float | None = None
    threshold: float | None = None


@dataclass
class LayerHealth:
    layer: str
    status: str               # "green" | "yellow" | "red"
    checks: list[HealthCheck] = field(default_factory=list)
    summary: str = ""


@dataclass
class AttentionItem:
    severity: str              # "critical" | "warning" | "info"
    layer: str
    message: str
    action: str | None = None


@dataclass
class OpsReport:
    date: str
    overall_status: str        # "green" | "yellow" | "red"
    layers: list[LayerHealth] = field(default_factory=list)
    needs_attention: list[AttentionItem] = field(default_factory=list)
    summary: str = ""


@dataclass
class Alert:
    severity: str              # "critical" | "warning" | "info"
    layer: str
    title: str
    detail: str
    action: str | None = None
    created_at: str = ""
    status: str = "open"       # "open" | "resolved" | "dismissed"


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    date: str
    start_time: str
    end_time: str | None = None
    status: str = "running"    # "running" | "success" | "partial" | "failed" | "skipped"
    error_msg: str = ""
    duration_s: float = 0.0
    version: str = ""
    config_hash: str = ""


@dataclass
class StartupReport:
    success: bool
    layers: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class PipelineRunResult:
    name: str
    status: str               # "success" | "partial" | "failed" | "skipped"
    error: str = ""
    elapsed_s: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class PipelineInfo:
    name: str
    schedule: str = ""
    timeout_s: int = 300
    max_retries: int = 1
    depends_on: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class VersionInfo:
    code_version: str
    code_branch: str
    config_hash: str
    data_version: str
    deployed_at: str


@dataclass
class PipelineStats:
    today_total: int = 0
    today_success: int = 0
    today_failed: int = 0
    today_partial: int = 0    # DL-43: 部分成功单列, 不再混入 success
    success_rate: float = 0.0


@dataclass
class AlertStats:
    open_count: int = 0
    escalated_count: int = 0


@dataclass
class OpsDashboard:
    date: str
    health: str
    pipelines: PipelineStats
    alerts: AlertStats
    version: str
    llm_costs: dict = field(default_factory=dict)
    uptime_days: int = 0
