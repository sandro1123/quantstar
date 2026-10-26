"""OPS-02: 数据管道超时治理 + 下游止血 — can_run 数据探针行为边界

08-18~20 事故路径: daily_data_pipeline 连续 timeout 判 failed, 但 kline 已落库,
下游 factor_update 被依赖检查全链挡死。测"数据在就放行"原则的边界:
- kline 已落库 + 管道 failed → 放行 (dep_met_via_data_probe)
- kline 行数不足 → 仍拦截
- 探针开关关闭 → 回退旧行为 (子表计数)
- 子表≥3 旧路径仍有效 (回归)
"""

from common.database import Database
from common.utils import bj_now
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator(config=None):
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db), config)
    orch._pipeline_configs = {
        "factor_update": {"depends_on": ["daily_data_pipeline"]},
    }
    return db, orch


def _mark_dep_failed(db, error_msg="timeout after 2400s"):
    """模拟 daily_data_pipeline 当日 failed (timeout 同样落 failed)。"""
    today = bj_now().split(" ")[0]
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, end_time, status, error_msg) "
        "VALUES ('ddp_failed', 'daily_data_pipeline', ?, '15:30:00', '16:10:00', 'failed', ?)",
        (today, error_msg),
    )
    conn.commit()


def _seed_klines(db, n):
    today = bj_now().split(" ")[0]
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    conn.executemany(
        "INSERT INTO daily_klines(code, date, close) VALUES (?, ?, 10.0)",
        [(str(i).zfill(6), today) for i in range(n)],
    )
    conn.commit()


def test_kline_landed_pipeline_failed_allows_downstream():
    """核心事故路径: kline 5541 行已落库 + 管道 timeout failed → factor_update 放行。"""
    db, orch = _orchestrator()
    _mark_dep_failed(db)
    _seed_klines(db, 5541)

    ok, reason = orch.can_run("factor_update")

    assert ok
    assert "dep_met_via_data_probe" in reason
    db.close()


def test_kline_insufficient_still_blocked():
    """边界: kline 行数低于 min_kline_rows → 不放行 (防残缺数据假放行)。"""
    db, orch = _orchestrator()
    _mark_dep_failed(db)
    _seed_klines(db, 100)  # 远低于 4000 阈值

    ok, reason = orch.can_run("factor_update")

    assert not ok
    assert "failed today" in reason
    db.close()


def test_probe_disabled_falls_back_to_subtable_count():
    """回滚开关: ops.dep_data_probe.enabled=False → kline 探针不生效, 回退子表计数。"""

    class _Cfg:
        def get(self, key, default=None):
            if key == "ops.dep_data_probe.enabled":
                return False
            return default

    db, orch = _orchestrator(config=_Cfg())
    _mark_dep_failed(db)
    _seed_klines(db, 5541)  # kline 够但探针关了

    ok, reason = orch.can_run("factor_update")

    assert not ok  # 子表也没数据 → 拦截
    db.close()


def test_subtable_fallback_still_works():
    """回归: 旧路径 (子表≥3) 不受影响。"""
    db, orch = _orchestrator()
    _mark_dep_failed(db, error_msg="some source error")
    today = bj_now().split(" ")[0]
    conn = db.get_connection()
    for t in ["stock_valuation", "stock_moneyflow", "concept_daily"]:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (code TEXT, date TEXT)")
        conn.execute(f"INSERT INTO {t} VALUES ('000001', ?)", (today,))
    conn.commit()

    ok, reason = orch.can_run("factor_update")

    assert ok
    assert "sub-tables" in reason
    db.close()


def test_success_dependency_unaffected():
    """回归: 依赖 success 时正常放行, 不触发探针。"""
    db, orch = _orchestrator()
    today = bj_now().split(" ")[0]
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, end_time, status) "
        "VALUES ('ddp_ok', 'daily_data_pipeline', ?, '15:30:00', '15:50:00', 'success')",
        (today,),
    )
    conn.commit()

    ok, reason = orch.can_run("factor_update")

    assert ok
    assert reason == "all dependencies met"
    db.close()
