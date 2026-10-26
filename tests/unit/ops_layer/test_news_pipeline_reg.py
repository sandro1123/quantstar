"""AG-NEWS 管道注册面补齐 (统筹 sm-d7b43861, 建了没通电复发案收口).

E 席件: news_per_stock 三件 — ops.yaml 定义 + orchestrator handler 映射
+ schedule_check 对账面。判例: 凡新管道交付, 验收锚必含三件。

红-绿: 撤回三件任一, 本文件对应测必红。
"""
import sqlite3
from unittest.mock import MagicMock, patch


class TestOpsYamlDefinition:
    def test_news_per_stock_in_ops_pipelines(self):
        """get_section 重建嵌套 dict (cfg_get 只回标量, 判例 test_ic_horizon)。"""
        from config_layer import Config
        c = Config()
        c.load()
        pipelines = c.get_section("ops.pipelines")
        assert "news_per_stock" in pipelines, "ops.yaml 缺 news_per_stock 定义"
        entry = pipelines["news_per_stock"]
        assert entry.get("schedule") == "17:00"
        assert entry.get("days") == "1-5"
        assert "daily_data_pipeline" in (entry.get("depends_on") or [])
        assert int(entry.get("timeout_s", 0)) == 300

    def test_emotion_cycle_in_ops_pipelines(self):
        """F 席件同窗验证 (两件同窗交付, 对账面互证)。"""
        from config_layer import Config
        c = Config()
        c.load()
        pipelines = c.get_section("ops.pipelines")
        assert "emotion_cycle" in pipelines, "ops.yaml 缺 emotion_cycle 定义"


class TestOrchestratorHandler:
    def test_dispatch_has_news_per_stock(self):
        """_execute_pipeline dispatch 必须含 news_per_stock 键。"""
        import inspect
        from ops_layer import orchestrator as orch
        src = inspect.getsource(orch.PipelineOrchestrator._execute_pipeline)
        assert '"news_per_stock": self._run_news_per_stock' in src, (
            "dispatch 缺 news_per_stock handler 映射 — 挂 crontab 必炸 "
            "pipeline not defined"
        )

    def test_dispatch_has_emotion_cycle(self):
        import inspect
        from ops_layer import orchestrator as orch
        src = inspect.getsource(orch.PipelineOrchestrator._execute_pipeline)
        assert '"emotion_cycle": self._run_emotion_cycle' in src


class TestNewsPerStockHandler:
    def _orch(self):
        from ops_layer import orchestrator as orch
        app = MagicMock()
        orch_obj = orch.PipelineOrchestrator.__new__(orch.PipelineOrchestrator)
        orch_obj._get_app = lambda: app
        return orch_obj, app

    def test_handler_passes_pool_codes_to_pipeline(self):
        """_run_news_per_stock: 候选池从 stock_pool 昨荐+活仓取,
        经 PipelineInput.symbols 透传给 stock_news 管线。"""
        orch_obj, app = self._orch()
        captured = {}

        def fake_run(name, inp):
            captured["name"] = name
            captured["symbols"] = inp.symbols
            res = MagicMock()
            res.status = "success"
            res.records_written = 12
            res.errors = []
            res.metadata = {}
            return res

        app.data.run_pipeline.side_effect = fake_run

        # stock_pool 候选池查询 mock (昨荐+活仓 ~30 只)
        def fake_conn_execute(sql, *args):
            if "stock_pool" in sql:
                m = MagicMock()
                m.fetchall.return_value = [("300409",), ("000001",)]
                return m
            return MagicMock()

        app.data.db.get_connection.return_value.execute.side_effect = fake_conn_execute

        result = orch_obj._run_news_per_stock({"date": "2026-09-13"})
        assert captured["name"] == "stock_news"
        assert captured["symbols"], "symbols 空 — per_stock_em 腿不会拉"
        assert isinstance(captured["symbols"], list)

    def test_handler_empty_pool_honest_skip(self):
        """候选池空 (周末/清池) → 诚实 skip 非假 success 记录数。"""
        orch_obj, app = self._orch()

        def fake_run(name, inp):
            res = MagicMock()
            res.status = "success"
            res.records_written = 0
            res.errors = []
            res.metadata = {}
            return res

        app.data.run_pipeline.side_effect = fake_run
        m = MagicMock()
        m.fetchall.return_value = []
        app.data.db.get_connection.return_value.execute.return_value = m

        result = orch_obj._run_news_per_stock({"date": "2026-09-13"})
        meta = getattr(result, "metadata", {}) or {}
        assert meta.get("skipped") is True or result.success is False, (
            "空池须诚实 skip, 不许假 success"
        )


class TestScheduleCheckFace:
    def test_schedule_drift_recognizes_new_pipeline(self):
        """对账面: news_per_stock 定义入 ops.yaml 后, schedule_check 对
        假 crontab (含挂行) 判 covered; 缺行判 missing。"""
        from ops_layer.schedule_check import check_schedule_drift
        pipelines = {
            "news_per_stock": {"schedule": "17:00"},
            "emotion_cycle": {"schedule": "16:35"},
        }
        with_line = "0 17 * * 1-5 pipeline run news_per_stock >> log\n"
        r = check_schedule_drift(pipelines, with_line)
        assert "news_per_stock" in r["covered"]
        assert "emotion_cycle" in r["missing"]

    def test_full_config_pipelines_load(self):
        """全量 ops.pipelines 可载 — 新条目 yaml 语法零破坏。"""
        from config_layer import Config
        c = Config()
        c.load()
        all_p = c.get_section("ops.pipelines")
        assert "news_per_stock" in all_p
        assert "emotion_cycle" in all_p
