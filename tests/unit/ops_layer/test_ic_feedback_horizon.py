"""fwd_10d: ic_feedback horizon 可配 — 默认 5 行为不变 / 10d 可选 / lag 自动抬升

:memory: 库, 无生产写。ic_feedback 是本任务的真正目的 (加列没人用=白做)。
"""
import json
import os
import sys

import pytest

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
    """12 只股票, 因子分与收益正相关 → IC 应为正。"""
    conn = db.get_connection()
    for i in range(12):
        code = "%06d" % i
        score = float(i)
        ret = float(i) * 0.1
        conn.execute(
            f"INSERT INTO factor_daily (date, code, factor_scores, {fwd_col}) "
            "VALUES (?, ?, ?, ?)",
            (date, code, json.dumps({"roe_quality": score}), ret))
    conn.commit()


def _seed_ic_rows_span(db, fwd_col, n_days=15):
    """按日历日铺最近 n_days 天的种子 — handler 的 target_date 取决于运行当日,
    测试不得依赖「今天是几号」; 铺满区间后任意 lag 计算都命中。"""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    for k in range(n_days):
        _seed_ic_rows(db, (today - _td(days=k)).strftime("%Y-%m-%d"), fwd_col)


def test_ic_feedback_default_horizon_is_5():
    db, orch = _mk_orch()
    # CF: 硬编码日期 2026-08-19 会随运行当日滚动出 target_date 窗口 (今日 target=08-20)
    # → 0 行提前 skip。改用 _seed_ic_rows_span 按 today 铺种子, 测试不依赖"今天是几号"。
    _seed_ic_rows_span(db, "fwd_return_5d")
    result = orch._run_ic_feedback({})
    assert result.success is True
    assert result.metadata["status"] == "completed"
    assert result.metadata["n_factors"] >= 1
    db.close()


def test_ic_feedback_horizon_10_reads_10d_column():
    db, orch = _mk_orch()
    # 只有 10d 列有值 → 若 handler 仍读 5d 会 skipped, 读 10d 则 completed
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({"strategy.ic_horizon": 10})
    assert result.success is True
    assert result.metadata["status"] == "completed", result.metadata
    assert result.metadata["n_stocks"] == 12
    db.close()


def test_ic_feedback_lag_raised_to_horizon():
    """lag < horizon 时 handler 自动抬到 horizon (warning 留痕), 命中已铺种子。"""
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({"strategy.ic_horizon": 10,
                                    "strategy.ic_lag_days": 3})
    assert result.metadata["status"] == "completed", result.metadata
    db.close()


def test_ic_feedback_unknown_horizon_falls_back_to_5():
    db, orch = _mk_orch()
    # CF: 硬编码日期 2026-08-19 会随运行当日滚动出 target_date 窗口 (今日 target=08-20)
    # → 0 行提前 skip。改用 _seed_ic_rows_span 按 today 铺种子, 测试不依赖"今天是几号"。
    _seed_ic_rows_span(db, "fwd_return_5d")
    result = orch._run_ic_feedback({"strategy.ic_horizon": 7})
    assert result.success is True
    assert result.metadata["status"] == "completed"
    db.close()


def test_ic_feedback_insufficient_rows_skips_cleanly():
    db, orch = _mk_orch()
    result = orch._run_ic_feedback({"strategy.ic_horizon": 10})
    assert result.success is True
    assert result.metadata["status"] == "skipped"
    db.close()
