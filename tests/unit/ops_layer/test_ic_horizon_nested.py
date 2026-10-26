"""Q3 回归: ic_horizon 嵌套形态读取 (生产路径 get_section 重建嵌套 dict)。

c6a602b 的 handler 只读扁平点键 "strategy.ic_horizon", 但生产 cfg 来自
get_section("ops.pipelines") — 它把点键重建为嵌套 dict, 故 yaml 配置到达
handler 时是 cfg["strategy"]["ic_horizon"]。旧代码恒读不到 → 恒回落 5,
开关是死的 (单测直传扁平 dict 绕过 get_section 才显绿)。

本测试直传嵌套 dict 模拟生产形态, 断言 handler 读到 10d。
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _mk_orch():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT, "
        "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_10d REAL, "
        "fwd_return_20d REAL, PRIMARY KEY (date, code))")
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _seed_ic_rows(db, date, fwd_col):
    conn = db.get_connection()
    for i in range(12):
        code = "%06d" % i
        conn.execute(
            f"INSERT INTO factor_daily (date, code, factor_scores, {fwd_col}) "
            "VALUES (?, ?, ?, ?)",
            (date, code, json.dumps({"roe_quality": float(i)}), float(i) * 0.1))
    conn.commit()


def _seed_ic_rows_span(db, fwd_col, n_days=15):
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    for k in range(n_days):
        _seed_ic_rows(db, (today - _td(days=k)).strftime("%Y-%m-%d"), fwd_col)


def test_ic_feedback_nested_cfg_shape_reads_10d():
    """生产形态: get_section 重建的嵌套 dict {"strategy": {"ic_horizon": 10}}。

    只有 10d 列有值 → 若 handler 读不到嵌套键会回落 5d → skipped;
    读到嵌套键则走 10d → completed。
    """
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})
    assert result.success is True
    assert result.metadata["status"] == "completed", result.metadata
    assert result.metadata["n_stocks"] == 12
    db.close()


def test_ic_feedback_nested_lag_raised():
    """嵌套形态下 lag < horizon 仍自动抬升。"""
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback(
        {"strategy": {"ic_horizon": 10, "ic_lag_days": 3}})
    assert result.metadata["status"] == "completed", result.metadata
    db.close()


def test_ic_feedback_flat_shape_still_works():
    """扁平形态 (测试直传契约) 不被破坏。"""
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({"strategy.ic_horizon": 10})
    assert result.metadata["status"] == "completed", result.metadata
    db.close()
