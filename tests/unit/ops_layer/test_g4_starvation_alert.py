"""G4 依赖饿死告警 — scoped 单测 (隔离库, 铁律 #18 负向对照)。

跑法: PYTHONPATH=src pytest tests/ops_layer/test_g4_starvation_alert.py -q
覆盖: parse_starvation 五分支判定表 + build_starvation_alert 字段/severity 规则
      + _alert_starvation 集成 (隔离 SQLite 内存库, 不碰生产库)。
"""

import sqlite3

import pytest

from ops_layer.g4_starvation_alert import build_starvation_alert, parse_starvation


class TestParseStarvation:
    def test_still_running(self):
        r = parse_starvation("dependency 'daily_data_pipeline' is still running (300s)")
        assert r == {"dep": "daily_data_pipeline", "kind": "running", "wait_s": None}

    def test_not_run_today(self):
        r = parse_starvation("dependency 'factor_update' has not run today")
        assert r == {"dep": "factor_update", "kind": "not_run", "wait_s": None}

    def test_skipped_cascade(self):
        r = parse_starvation("dependency 'factor_update' status is 'skipped'")
        assert r == {"dep": "factor_update", "kind": "skipped", "wait_s": None}

    def test_wait_timeout(self):
        r = parse_starvation(
            "dependencies not completed within 900s timeout")
        assert r is not None
        assert r["kind"] == "wait_timeout"
        assert r["wait_s"] == 900

    # 负向对照 #1: failed 不触发饿死告警 (OPS-07 已覆盖, 防双发)
    def test_failed_returns_none(self):
        assert parse_starvation("dependency 'daily_data_pipeline' failed today") is None

    def test_no_dependencies_returns_none(self):
        assert parse_starvation("no dependencies") is None

    def test_empty_reason_returns_none(self):
        assert parse_starvation("") is None

    def test_all_dependencies_met_returns_none(self):
        assert parse_starvation("all dependencies met") is None


class TestBuildStarvationAlert:
    def test_basic_warning_fields(self):
        fields = build_starvation_alert(
            "ic_feedback", "dependency 'factor_update' status is 'skipped'",
            starved_today=["ic_feedback"],
        )
        # 饿死链含 factor_update (上游) → 按规格升 critical
        assert fields["severity"] == "critical"
        assert fields["layer"] == "ops"
        assert "依赖饿死: ic_feedback ← factor_update" in fields["title"]

    def test_plain_warning_when_no_factor_update(self):
        fields = build_starvation_alert(
            "quality_check", "dependency 'market_feed' is still running",
            starved_today=["quality_check"],
        )
        assert fields["severity"] == "warning"
        assert "is still running" in fields["detail"]

    def test_critical_when_factor_update_in_chain(self):
        # 饿死链含 factor_update → critical (因子供给是全链咽喉)
        fields = build_starvation_alert(
            "evening_report", "dependency 'factor_update' status is 'skipped'",
            starved_today=["evening_report"],
        )
        assert fields["severity"] == "critical"

    def test_critical_when_three_or_more_starved(self):
        fields = build_starvation_alert(
            "quality_check", "dependency 'daily_data_pipeline' is still running",
            starved_today=["quality_check", "freshness_audit", "daily_backtest"],
        )
        assert fields["severity"] == "critical"

    def test_title_contains_pipeline_for_dedup(self):
        # AlertManager.should_alert 按 title 去重 → title 必须含管道名 (负向对照 #3)
        a = build_starvation_alert("pipeline_a", "dependency 'x' has not run today")
        b = build_starvation_alert("pipeline_b", "dependency 'x' has not run today")
        assert a["title"] != b["title"]
        assert "pipeline_a" in a["title"]

    def test_action_mentions_zombie_criteria(self):
        fields = build_starvation_alert("p", "dependency 'd' is still running")
        assert "G7-A" in (fields.get("action") or "")


class TestAlertStarvationIntegration:
    """_alert_starvation 端到端: 隔离内存库, 验证 alerts 表落行 + skip 返回路径不被阻断。"""

    @pytest.fixture()
    def orchestrator_with_memory_db(self, tmp_path):
        from ops_layer.alert_manager import AlertManager
        from ops_layer.core.models import Alert
        from ops_layer.orchestrator import PipelineOrchestrator

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE pipeline_runs (
            run_id TEXT, pipeline_name TEXT, date TEXT, start_time TEXT,
            end_time TEXT, status TEXT, error_msg TEXT, duration_s REAL,
            version TEXT, config_hash TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            severity TEXT, layer TEXT, title TEXT, detail TEXT,
            action TEXT, status TEXT DEFAULT 'open',
            resolved_at TEXT, created_at TEXT)""")

        orch = object.__new__(PipelineOrchestrator)

        class _DB:
            def get_connection(self):
                return conn

        class _AM(AlertManager):
            def __init__(self):
                self.sent = []

            def send(self, alert: Alert):  # 拦截飞书, 只记录
                self.sent.append(alert)
                return True

        orch.db = _DB()
        orch.alert_manager = _AM()
        return orch, conn

    def _today(self):
        from common.utils import bj_now
        return bj_now().split(" ")[0]

    def test_running_dependency_triggers_alert_row(self, orchestrator_with_memory_db):
        orch, conn = orchestrator_with_memory_db
        today = self._today()
        conn.execute(
            "INSERT INTO pipeline_runs VALUES ('r1','daily_data_pipeline',?, "
            "'2026-08-24 16:10:36',NULL,'running',NULL,NULL,NULL,NULL,NULL)",
            (today,))
        conn.commit()

        orch._alert_starvation(
            "quality_check", "dependency 'daily_data_pipeline' is still running")
        # mock send 拦截飞书只记录; 真实落库路径由 test_alert_manager.py 覆盖
        assert len(orch.alert_manager.sent) == 1
        alert = orch.alert_manager.sent[0]
        assert alert.severity == "warning"
        assert "依赖饿死: quality_check ← daily_data_pipeline" in alert.title
        # 上下文查询应带上游 run 信息 (dep 最新行 running)
        assert "r1" in alert.detail

    # 负向对照 #1 (集成层): failed 依赖不产生任何告警行
    def test_failed_dependency_no_alert(self, orchestrator_with_memory_db):
        orch, conn = orchestrator_with_memory_db
        orch._alert_starvation(
            "ic_feedback", "dependency 'daily_data_pipeline' failed today")
        assert len(orch.alert_manager.sent) == 0

    # 负向对照 #2 (集成层): success 终态路径根本不会进入本函数 — 这里验证
    # all-met reason 同样静默
    def test_success_reason_no_alert(self, orchestrator_with_memory_db):
        orch, conn = orchestrator_with_memory_db
        orch._alert_starvation("ic_feedback", "all dependencies met")
        assert len(orch.alert_manager.sent) == 0

    # 告警失败不阻断主流程 (fail-safe)
    def test_exception_does_not_propagate(self, orchestrator_with_memory_db):
        orch, _conn = orchestrator_with_memory_db
        orch.db = type("Broken", (), {"get_connection": lambda s: (_ for _ in ()).throw(RuntimeError("boom"))})()
        # 不抛异常即通过
        orch._alert_starvation("p", "dependency 'd' is still running")


class TestWaitTimeoutDepName:
    """Q3: wait_timeout 超时文案应带出阻塞依赖名 (告警标题不再"未知上游")。"""

    def test_parse_starvation_timeout_with_dep(self):
        """parse_starvation 能从带依赖名的超时文案提取 dep。"""
        r = parse_starvation(
            "dependencies not completed within 900s timeout (dependency 'factor_update')")
        assert r is not None
        assert r["kind"] == "wait_timeout"
        assert r["wait_s"] == 900
        assert r["dep"] == "factor_update"

    def test_build_alert_timeout_title_has_dep(self):
        """build_starvation_alert 超时场景标题含依赖名。"""
        fields = build_starvation_alert(
            "ic_feedback",
            "dependencies not completed within 900s timeout (dependency 'factor_update')",
            starved_today=["ic_feedback"],
        )
        assert "factor_update" in fields["title"]
        assert "未知上游" not in fields["title"]

    def test_wait_for_dependencies_timeout_includes_dep(self):
        """wait_for_dependencies 超时返回的 reason 含阻塞依赖名。"""
        from unittest.mock import patch, MagicMock
        from ops_layer.orchestrator import PipelineOrchestrator

        mock_db = MagicMock()
        mock_db.get_connection.return_value = MagicMock()
        orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
        orch._pipeline_configs = {"child": {"depends_on": ["dep"], "timeout_s": 300}}
        orch.db = mock_db

        # mock can_run 始终返回 "still running"
        with patch.object(orch, 'can_run', return_value=(False, "dependency 'dep' is still running (300s)")):
            with patch('time.sleep'):
                ok, reason = orch.wait_for_dependencies("child", max_wait_s=60)

        assert not ok
        assert "dependency 'dep'" in reason, f"超时文案缺依赖名: {reason}"


class TestGhostSixthBranch:
    """Q4: ghost 持锁→下游 skip→两边同时告警的形态钉住。
    ghost 告警与饿死告警并存=正确行为(非双发); parse_starvation 对 ghost
    告警文案天然返回 None, 两机制互不干扰。实例: 08-25 id45/46+id47/48。"""

    def test_ghost_alert_title_not_parsed_as_starvation(self):
        """ghost 告警 title 不含 dependency 格式 → parse_starvation 返回 None。"""
        assert parse_starvation("Ghost 线程: daily_data_pipeline") is None

    def test_ghost_alert_detail_not_parsed_as_starvation(self):
        """ghost 告警 detail 不含 dependency 格式 → 不被误判为饿死。"""
        detail = ("管道超时 2700s 后线程仍在运行 (cancel() 无效), "
                  "可能持续持有 DB 写锁; 已登记 ghost_registry")
        assert parse_starvation(detail) is None

    def test_ghost_and_starvation_coexist_semantics(self):
        """ghost critical + 下游饿死并存时, 饿死告警仍按既有规则构造。
        (severity 不因 ghost 存在而新增规则)"""
        fields = build_starvation_alert(
            "dev_verify_report",
            "dependency 'evening_report' status is 'skipped'",
            starved_today=["dev_verify_report", "daily_ops_report"],
        )
        # 2条饿死 < 3 且不含 factor_update → warning (不因 ghost 升级)
        assert fields["severity"] == "warning"
        assert "evening_report" in fields["title"]
