"""Regression tests for truthful pipeline orchestration."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def test_configured_dependencies_are_enforced():
    db, orchestrator = _orchestrator()
    orchestrator._pipeline_configs = {
        "factor_update": {"depends_on": ["daily_data_pipeline"]}
    }

    can_run, reason = orchestrator.can_run("factor_update")

    assert not can_run
    assert "has not run today" in reason
    db.close()


def test_unknown_handler_cannot_report_success():
    db, orchestrator = _orchestrator()
    orchestrator._pipeline_configs = {
        "unimplemented": {"max_retries": 0, "timeout_s": 1}
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)

    result = orchestrator.run_pipeline("unimplemented")

    assert result.status == "failed"
    assert "no execution handler" in result.error
    db.close()


def test_failed_data_subpipeline_fails_parent_run(monkeypatch):
    db, orchestrator = _orchestrator()
    orchestrator._pipeline_configs = {
        "daily_data_pipeline": {
            "max_retries": 0,
            "timeout_s": 2,
            "start_date": "2026-07-28",
            "end_date": "2026-07-28",
        }
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)

    def run_pipeline(name, pipeline_input):
        # 关键子管道（valuation）失败必须使父管道失败（truthfulness）；
        # moneyflow 为 best-effort，其失败降级为 warning 不阻塞关键路径。
        status = "failed" if name == "valuation" else "success"
        errors = ["source unavailable"] if status == "failed" else []
        return PipelineResult(
            name=name,
            date=pipeline_input.date,
            status=status,
            records_written=0 if status == "failed" else 10,
            errors=errors,
        )

    data_layer = SimpleNamespace(db=db, run_pipeline=run_pipeline)
    orchestrator.set_app(SimpleNamespace(data=data_layer))
    # SQLite keeps one in-memory connection; run synchronously so this test validates
    # orchestration semantics rather than SQLite's cross-thread transaction behavior.
    orchestrator._executor.shutdown(wait=True)
    orchestrator._executor = ThreadPoolExecutor(max_workers=1)

    result = orchestrator.run_pipeline("daily_data_pipeline")

    assert result.status == "failed"
    assert "valuation" in result.error
    row = db.get_connection().execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE pipeline_name = ?",
        ("daily_data_pipeline",),
    ).fetchone()
    assert row["status"] == "failed"
    assert "valuation" in row["error_msg"]
    db.close()


def test_best_effort_moneyflow_failure_does_not_fail_parent(monkeypatch):
    db, orchestrator = _orchestrator()
    orchestrator._pipeline_configs = {
        "daily_data_pipeline": {
            "max_retries": 0,
            "timeout_s": 2,
            "start_date": "2026-07-28",
            "end_date": "2026-07-28",
        }
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)

    def run_pipeline(name, pipeline_input):
        # moneyflow 端点被限流时失败 → 父管道不判 failed (best-effort 降级)。
        # fail-fast v1 (sched-fix-design-D §4.4): 不再静默 success, 升级 partial —
        # 数据已落库但降级, can_run 对 partial 放行, 下游不阻塞。
        status = "failed" if name == "moneyflow" else "success"
        errors = ["source unavailable"] if status == "failed" else []
        return PipelineResult(
            name=name,
            date=pipeline_input.date,
            status=status,
            records_written=0 if status == "failed" else 10,
            errors=errors,
        )

    data_layer = SimpleNamespace(db=db, run_pipeline=run_pipeline)
    orchestrator.set_app(SimpleNamespace(data=data_layer))
    orchestrator._executor.shutdown(wait=True)
    orchestrator._executor = ThreadPoolExecutor(max_workers=1)

    result = orchestrator.run_pipeline("daily_data_pipeline")

    # 核心不变量: best-effort 失败不得把父管道拖成 failed (不阻塞下游)。
    # fail-fast v1 后状态从静默 success 升级为 partial (可观测降级)。
    assert result.status == "partial"
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name = ?",
        ("daily_data_pipeline",),
    ).fetchone()
    assert row["status"] == "partial"
    db.close()


def test_kline_failure_with_existing_rows_is_partial_success(monkeypatch):
    # kline 报失败但当日行情实际已落库 → 父管道 partial success，
    # 避免源未失效时因超时/尾部个别股票失败而整体 failed。
    db, orchestrator = _orchestrator()
    orchestrator._pipeline_configs = {
        "daily_data_pipeline": {
            "max_retries": 0,
            "timeout_s": 2,
            "start_date": "2026-07-29",
            "end_date": "2026-07-29",
        }
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)

    # 模拟当日 kline 已落库 5000 行
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_klines "
        "(code TEXT, date TEXT, close REAL)"
    )
    conn.executemany(
        "INSERT INTO daily_klines(code, date, close) VALUES (?, ?, 10.0)",
        [(str(i).zfill(6), "2026-07-29") for i in range(5000)],
    )
    conn.commit()

    def run_pipeline(name, pipeline_input):
        if name == "kline":
            return PipelineResult(
                name=name,
                date=pipeline_input.date,
                status="failed",
                records_written=5000,
                errors=["kline: 2 symbols returned empty"],
            )
        return PipelineResult(
            name=name,
            date=pipeline_input.date,
            status="success",
            records_written=10,
            errors=[],
        )

    data_layer = SimpleNamespace(db=db, run_pipeline=run_pipeline)
    orchestrator.set_app(SimpleNamespace(data=data_layer))
    orchestrator._executor.shutdown(wait=True)
    orchestrator._executor = ThreadPoolExecutor(max_workers=1)

    result = orchestrator.run_pipeline("daily_data_pipeline")

    # DL-43: kline 失败但当日行情已落库 → 父管道 partial (不再是假 success)。
    # partial 独立落 pipeline_runs.status='partial' 并发 warning 告警。
    assert result.status == "partial"
    assert result.metadata.get("partial") is True
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name = ?",
        ("daily_data_pipeline",),
    ).fetchone()
    assert row["status"] == "partial"
    db.close()



# 被测面取 date=今天; 夹具必须与它时间同源, 否则测试自带保质期
_TODAY = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

class TestHollowGuardChecksColumnValues:
    """N3 配套 (2026-09-14): 防空心守卫口径换代。

    病理: _run_factor_update 的守卫只数行 (COUNT(*))。2026-07-21 聚合器
    20→89 因子换代后写入形态改为只写 factor_scores JSON 列, 物理因子列
    (momentum_5d/pe_ttm...) 全库零写入 — 若未来某次换代只建行不写值
    (行在值空), 行数守卫照绿, 静默空面无人知。守卫必须双面: 行数 AND
    列值面 (factor_scores 非空行占比)。
    """

    def _harness(self, monkeypatch, tmp_path, rows, scores_nonnull):
        """构造文件库 + orchestrator + mock app, 夹具因子面由参数塑形。

        用文件库而非 :memory: (关键): handler 内 Database() 无参构造走
        QS_TEST_DB → 同一文件库; :memory: 库每连接独立, handler 端拿到的
        是另一个空库 (两库分裂 = 上一轮 no such table 的病根)。
        """
        db_path = str(tmp_path / "n3guard.db")
        db = Database(db_path)
        db.init_db()
        from ops_layer.core.tables import OPS_TABLES_DDL
        conn = db.get_connection()
        conn.executescript(OPS_TABLES_DDL)
        orchestrator = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT)")
        # 时点炸弹修 (09-15 亲踩): daily_klines 必须与 factor_daily 同日有行,
        # 否则 handler 的 date=max(factor,kline) 落到 bj_date()=今天 →
        # 夹具 09-14 行全 miss → hollow guard 误拦 (跨日即红, 审计官
        # Round2 「夹具钉死日期+被测码 now()」判例族)。
        conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-09-14')")
        conn.execute(
            "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT)")
        for i in range(rows):
            scores = '{"momentum_20d": 0.1}' if i < scores_nonnull else None
            conn.execute(
                # 时点同源 (Round2 复审): 被测面 _run_factor_update 查的是
                # date=今天; 夹具原先钉死 '2026-09-14' -> 只在那一天绿,
                # 09-15 起 hollow guard 判 "factor_daily empty for <今天>"。
                # 与 R2-04(test_handler_contract) 同族同日同形态。
                f"INSERT INTO factor_daily VALUES ('{_TODAY}', ?, ?)",
                (f"00000{i}", scores))
        conn.commit()

        # _run_factor_update 内 Database() 无参构造 → QS_TEST_DB 环境变量优先
        # (Database.__init__ 设计即此, conftest 会话库同机制)。把夹具库路径
        # 写进 QS_TEST_DB = handler 读到的就是这个库, 无需 patch Database 类。
        # (判例: 环境变量是 Database 路径解析的官方测试锚, 比类级 patch 干净)
        import os as _os
        monkeypatch.setenv("QS_TEST_DB", db.db_path)
        import bootstrap as boot_mod
        app = SimpleNamespace(
            strategy=SimpleNamespace(
                run_daily=lambda *a, **k: SimpleNamespace(signals=[])),
            execution=None)
        monkeypatch.setattr(boot_mod, "bootstrap_app", lambda: app,
                            raising=True)
        return db, orchestrator, app

    def test_all_rows_hollow_scores_fails_guard(self, monkeypatch, tmp_path):
        """行在但 factor_scores 全 NULL (空心面) → 判 failed, 不许假 success。"""
        db, orchestrator, app = self._harness(monkeypatch, tmp_path, rows=100, scores_nonnull=0)
        result = orchestrator._run_factor_update({})
        assert result.success is False
        assert "hollow" in " ".join(result.errors).lower() or any(
            "factor_scores" in str(e) for e in result.errors)
        db.close()

    def test_majority_real_scores_passes_guard(self, monkeypatch, tmp_path):
        """主体行有真 scores (生产形态) → 守卫放行 success。"""
        db, orchestrator, app = self._harness(monkeypatch, tmp_path, rows=100, scores_nonnull=95)
        result = orchestrator._run_factor_update({})
        assert result.success is True
        db.close()

    def test_half_hollow_borderline_fails(self, monkeypatch, tmp_path):
        """恰好半数空 = 病态面 (低于 50% 阈值即红) — 保守判 failed。"""
        db, orchestrator, app = self._harness(monkeypatch, tmp_path, rows=100, scores_nonnull=40)
        result = orchestrator._run_factor_update({})
        assert result.success is False
        db.close()
