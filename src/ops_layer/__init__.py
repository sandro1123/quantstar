"""ops_layer/__init__.py — OpsLayer 入口

Ops Layer: 系统运维 — 健康检查 / 管道编排 / 管道日志 / 告警 / 版本追踪 / 报告 / 启动管理
"""

from __future__ import annotations

from common.database import Database
from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from common.utils import bj_date
from config_layer import Config
from ops_layer.alert_manager import AlertManager
from ops_layer.anomaly_detector import AnomalyDetector
from ops_layer.auto_healer import AutoHealer
from ops_layer.chaos import ChaosEngine
from ops_layer.core.models import (
    Alert,
    AlertStats,
    LayerHealth,
    OpsDashboard,
    OpsReport,
    PipelineInfo,
    PipelineRunResult,
    PipelineStats,
    StartupReport,
    VersionInfo,
)
from ops_layer.health import OpsHealth
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger
from ops_layer.startup import StartupManager
from ops_layer.version_tracker import VersionTracker

logger = get_logger("ops")


class OpsLayer:
    def __init__(self, db: Database | None = None, config: Config | None = None,
                 llm_service: object | None = None):
        import threading
        self.db = db or Database()
        self.config = config
        # AG-10: 复用 bootstrap 注入的门内叙述柜台, 不现场建 ProviderManager (避免双 key 池)
        self._llm_service = llm_service
        self._pipeline_lock = threading.RLock()
        self._ops_health = OpsHealth({})
        self.pipeline_logger = PipelineLogger(self.db)
        self.anomaly_detector = AnomalyDetector(self.db)
        self.auto_healer = AutoHealer(self.db)
        self.chaos = ChaosEngine(self.db)
        self.alert_manager = AlertManager(self.db)
        self.version_tracker = VersionTracker(self.db)
        self.startup_manager = StartupManager(self.db, self.pipeline_logger, self.alert_manager)
        self.orchestrator = PipelineOrchestrator(self.db, self.pipeline_logger, self.alert_manager, config)
        self._started = False

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=30)
    def start(self):
        if not self._started:
            from ops_layer.core.tables import OPS_TABLES_DDL
            conn = self.db.get_connection()
            conn.executescript(OPS_TABLES_DDL)
            conn.commit()
            from execution_layer.migrations import ensure_schema
            ensure_schema(self.db)
            self._load_pipeline_defs()
            # OPS-06: bootstrap 完成时记录版本 (record_version 内部当日去重,
            # 防每 5 分钟 cron 刷爆 version_history); 失败不阻断启动
            try:
                self.version_tracker.record_version()
            except Exception as e:
                logger.warning(f"version record skipped (fail-safe): {e}")
            self._started = True

    def _load_pipeline_defs(self):
        if not self.config:
            return
        try:
            pipelines = self.config.get_section("ops.pipelines") if hasattr(self.config, "get_section") else {}
            if pipelines:
                self.orchestrator.load_pipelines({"ops": {"pipelines": pipelines}})
                logger.info(f"Loaded {len(pipelines)} pipeline definitions from config")
        except Exception as e:
            logger.warning(f"Failed to load pipeline defs from config: {e}")

    @catch_quantstar_error(layer_name="ops")
    def register_layers(self, layers: dict[str, object]):
        self._ops_health = OpsHealth(layers)

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def check_health(self) -> OpsReport:
        return self._ops_health.check_all()

    @catch_quantstar_error(layer_name="ops")
    def health(self) -> LayerHealth:
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []
        try:
            n_pipelines = len(self.orchestrator._pipeline_configs) if hasattr(self, 'orchestrator') else 0
            checks.append(HealthCheck(name="pipelines", status="pass" if n_pipelines > 0 else "warn",
                        detail=f"管道定义: {n_pipelines}"))
        except Exception as e:
            checks.append(HealthCheck(name="pipelines", status="warn", detail=f"管道检查失败: {e}"))
        try:
            active = self.pipeline_logger.get_active_runs()
            checks.append(HealthCheck(name="active_runs", status="pass" if len(active) == 0 else "warn",
                        detail=f"运行中: {len(active)} 个"))
        except Exception as e:
            checks.append(HealthCheck(name="active_runs", status="warn", detail=f"运行检查失败: {e}"))
        try:
            total, failed = 0, 0
            conn = self.db.get_connection()
            row = conn.execute("SELECT COUNT(*), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) FROM pipeline_runs").fetchone()
            total, failed = row[0] or 0, row[1] or 0
            rate = (total - failed) / max(total, 1)
            checks.append(HealthCheck(name="success_rate", status="pass" if rate > 0.8 else "warn",
                        detail=f"总运行 {total}, 成功率 {rate:.0%}"))
        except Exception as e:
            checks.append(HealthCheck(name="success_rate", status="warn", detail=f"统计失败: {e}"))
        overall = "green" if all(c.status == "pass" for c in checks) else "yellow" if any(c.status == "warn" for c in checks) else "red"
        return LayerHealth(layer="ops", status=overall, checks=checks,
            summary=f"Ops Layer {'正常' if overall == 'green' else '需关注'} ({len(checks)} 探针)")

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=5)
    def start_pipeline(self, pipeline_name: str) -> str:
        return self.pipeline_logger.start_run(pipeline_name)

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def end_pipeline(self, run_id: str, status: str, error: str = ""):
        self.pipeline_logger.end_run(run_id, status, error)

    @catch_quantstar_error(layer_name="ops")
    def run_pipeline(self, pipeline_name: str) -> PipelineRunResult:
        # 超时/重试/熔断由 orchestrator.run_pipeline 内建 executor + cfg.timeout_s 负责，
        # 此处不再叠加 with_timeout：避免第二个 executor 线程在超时后继续运行（ghost work）
        # 并在解释器退出时报 "cannot schedule new futures after interpreter shutdown"。
        with self._pipeline_lock:
            return self._run_pipeline_single(pipeline_name)

    def _zombie_reap_enabled(self) -> bool:
        """OPS-01 收割开关 (ops.zombie_reap.enabled, 默认开)。"""
        try:
            if self.config is not None and hasattr(self.config, "get"):
                v = self.config.get("ops.zombie_reap.enabled")
                if v is not None:
                    return bool(v)
            from config_layer import cfg_get
            v = cfg_get("ops.zombie_reap.enabled")
            if v is not None:
                return bool(v)
        except Exception:
            pass
        return True

    def _pipeline_timeout_lookup(self) -> dict[str, int]:
        """管道名 → timeout_s 映射, 供僵尸过期判定用。"""
        try:
            return {name: int(cfg.get("timeout_s", 300))
                    for name, cfg in self.orchestrator._pipeline_configs.items()}
        except Exception:
            return {}

    def _pipeline_full_cfg(self) -> dict[str, dict]:
        """管道名 → 完整配置映射 (判例 22), 供 reaper DL-65 ceiling 判定。"""
        try:
            return {name: dict(cfg)
                    for name, cfg in self.orchestrator._pipeline_configs.items()}
        except Exception:
            return {}

    def _run_pipeline_single(self, pipeline_name: str) -> PipelineRunResult:
        # OPS-01: 先收割过期 running 记录 (僵尸), 再判断是否跳过。
        # 保护机制不能只看 status — 僵尸 running 会把管道永久堵死 (08-17 事故)。
        # 裁定① (#94 sm-863edc87771d427d): 同名过滤 → 全量收割。月度管道
        # (model_retrain 等) 29 天无人同名再触发, 同名收割永远够不着其僵尸行
        # (2026-09-01 model_retrain 行挂 running→monthly_report 依赖饿死 900s
        # 实弹); 任意日常触发现在顺带清理全表过期 running。判定公式 (timeout+
        # 宽限) 与 _ZOMBIE_ERROR_MSG 零改 — 本体 reap_expired_runs 不传
        # pipeline_name (默认 None) 即全表, 窗口内 fresh 行不受影响。
        if self._zombie_reap_enabled():
            # 判例 22 同源修: 传 full_cfg (完整 DL-65 ceiling 含重试/等待窗)
            # — 裸 timeout_lookup 面已证误杀活行 (ddp 3000s<5550s 病历)。
            self.pipeline_logger.reap_expired_runs(
                timeout_lookup=self._pipeline_timeout_lookup(),
                full_cfg=self._pipeline_full_cfg(),
            )
        active = self.pipeline_logger.get_active_runs()
        for run in active:
            if run["pipeline_name"] == pipeline_name:
                logger.warning(f"Pipeline {pipeline_name} 已在运行 (run_id={run['run_id']}), 跳过重复触发")
                return PipelineRunResult(name=pipeline_name, status="skipped",
                                         error=f"pipeline already running ({run['run_id']})")
        return self.orchestrator.run_pipeline(pipeline_name)

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def list_pipelines(self) -> list[PipelineInfo]:
        return self.orchestrator.list_pipelines()

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def alert(self, severity: str, layer: str, title: str, detail: str, action: str = None) -> bool:
        a = Alert(severity=severity, layer=layer, title=title, detail=detail, action=action)
        return self.alert_manager.send(a)

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def version_info(self) -> VersionInfo:
        return self.version_tracker.record_version()

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=30)
    def startup(self, layer_instances: dict[str, object]) -> StartupReport:
        return self.startup_manager.startup(layer_instances)

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def shutdown(self, timeout: float = 10.0):
        active_runs = self.pipeline_logger.get_active_runs()
        for run in active_runs:
            # CHECK 卡 (2026-08-29): 不写 status='shutdown' — pipeline_runs.status
            # CHECK 白名单只有五态 (running/success/partial/failed/skipped), 半状态
            # 必撞 IntegrityError 且被装饰器静默吞 (run 残留 running)。run 被系统
            # 关闭打断 = 事实上的未完成 → 合法态 failed + 原因进 error_msg。
            self.pipeline_logger.end_run(
                run["run_id"], "failed",
                f"system shutdown: pipeline {run.get('pipeline_name', run['run_id'])} "
                "interrupted during shutdown")
        if hasattr(self, 'orchestrator') and self.orchestrator:
            try:
                self.orchestrator.shutdown(timeout)
            except Exception:
                pass
        logger.info(f"Ops Layer 关闭, 已结束 {len(active_runs)} 个运行中管道")
        return True

    @catch_quantstar_error(layer_name="ops")
    @with_timeout(timeout_s=10)
    def get_dashboard(self) -> OpsDashboard:
        health = self.check_health() if hasattr(self, '_ops_health') else None
        recent_runs = self.pipeline_logger.get_recent_runs(days=1)
        today_total = len(recent_runs)
        today_failed = sum(1 for r in recent_runs if getattr(r, "status", "") == "failed")
        today_success = sum(1 for r in recent_runs if getattr(r, "status", "") == "success")
        # DL-43: partial 单列, 不再混入 success (success_rate 保持严格口径)
        today_partial = sum(1 for r in recent_runs if getattr(r, "status", "") == "partial")
        open_alerts = self.alert_manager.get_open_alerts()
        escalated = self.alert_manager.check_escalation()
        version = self.version_tracker.record_version()
        # LLM 成本追踪 (AG-10: 经注入的叙述柜台 health() 取, 不现场建 ProviderManager)
        llm_costs = {}
        try:
            if self._llm_service is not None:
                h = self._llm_service.health()
                # AG-10 契约: health() 返回诚实 dict (非 dataclass), 用 .get() 读
                llm_costs = {
                    "providers_online": h.get("providers_online", 0),
                    "providers_total": h.get("providers_total", 0),
                    "degradation_tier": h.get("degradation_tier", ""),
                    "pool_health": h.get("pool_health", {}),
                }
            else:
                llm_costs = {"error": "not available"}
        except Exception:
            llm_costs = {"error": "not available"}
        return OpsDashboard(
            date=bj_date(),
            health=health.overall_status if health else "unknown",
            pipelines=PipelineStats(
                today_total=today_total,
                today_success=today_success,
                today_failed=today_failed,
                today_partial=today_partial,
                # DL-43: 严格口径 — partial 不计入 success_rate (部分成功不是成功)
                success_rate=round(today_success / max(1, today_total) * 100, 1),
            ),
            alerts=AlertStats(
                open_count=len(open_alerts),
                escalated_count=len(escalated),
            ),
            version=version.code_version if isinstance(version, VersionInfo) else "unknown",
            llm_costs=llm_costs,
            uptime_days=0,
        )
