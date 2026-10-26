"""G3 can_run 根治: stuck-running 跳过 + 回退终态行判定 — 行为边界测试。

最新行为 running 且超龄 (> timeout_s x 2, 与三判据 C3 同源) 时:
- 有最近终态行 → 按该终态判定 (success 放行 / failed 走 OPS-02 探针)
- 无任何终态行 → 保守拦截
- age == x2 整不跳过; 略超才跳过
- 正常 running (age <= x2) 行为不变: "is still running"
"""
import os
import sys

_QS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_QS_ROOT, "src"))

from datetime import datetime, timedelta
from common.utils import BJ_TZ, bj_date  # date 列=代码钟的今日, 不从 aged start_dt 派生 (跨午夜夹具病)

from common.database import Database
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
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    orch._pipeline_configs = {
        # dep timeout_s=60 → 超龄线 = 120s
        "factor_update": {"depends_on": ["dep_pipe"]},
        "dep_pipe": {"timeout_s": 60},
    }
    return db, orch


def _insert_run(db, run_id, status, start_dt, version=""):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time,"
        " end_time, status, version) VALUES (?, 'dep_pipe', ?, ?, ?, ?, ?)",
        (run_id, bj_date(),
         start_dt.strftime("%Y-%m-%d %H:%M:%S"), start_dt.strftime("%Y-%m-%d %H:%M:%S")
         if status != "running" else None, status, version),
    )
    conn.commit()


def _ago(seconds, now=None):
    # 必须用北京钟 (can_run 的 age 锚是 bj_now); datetime.now() 在 UTC runner 上差 8h → 假超龄 (CI 定谳)
    return (now or datetime.now(BJ_TZ)) - timedelta(seconds=seconds)


# ── stuck-running 判定边界 ─────────────────────────────────────────

def test_age_exactly_2x_timeout_not_skipped(db=None):
    """临界: age == x2 整不跳过 → 仍按 still running 拦截。"""
    db, orch = _orchestrator()
    now = datetime.now(BJ_TZ)
    _insert_run(db, "edge_eq", "running", _ago(120, now))  # 恰好 = 60*2
    ok, reason = orch.can_run("factor_update")
    assert not ok and "still running" in reason


def test_age_just_over_2x_with_success_history_passes():
    """略超 x2 + 历史有 success → 跳过僵尸行, 按 success 放行。"""
    db, orch = _orchestrator()
    now = datetime.now(BJ_TZ)
    _insert_run(db, "zombie", "running", _ago(121, now))
    _insert_run(db, "good_hist", "success", _ago(400, now), version="abc")
    ok, reason = orch.can_run("factor_update")
    assert ok and "stuck-running" in reason and "all dependencies met" in reason


def test_unparseable_start_time_conservative_block():
    """start_time 缺失/不可解析 → 无法判超龄, 保守走 still running。"""
    db, orch = _orchestrator()
    conn = db.get_connection()
    # date 必须用当天 (can_run 按 bj_now() 查当日行); 硬编码日期过期后
    # 查无行会落 "has not run today" 而非本用例要测的 still running 分支。
    today = bj_date()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status)"
        " VALUES ('bad_t', 'dep_pipe', ?, 'garbage', 'running')", (today,))
    conn.commit()
    ok, reason = orch.can_run("factor_update")
    assert not ok and "still running" in reason


# ── 回退终态判定 ──────────────────────────────────────────────────

def test_stuck_running_falls_back_to_failed_history_blocks():
    """超龄 running + 最近终态 failed → 按 failed 拦截 (探针另测, 此处验回退)。"""
    db, orch = _orchestrator()
    now = datetime.now(BJ_TZ)
    _insert_run(db, "zombie2", "running", _ago(6000, now))
    _insert_run(db, "fail_hist", "failed", _ago(7000, now))
    ok, reason = orch.can_run("factor_update")
    # 回退生效的证明: 拦截理由是 failed today 而非 still running (僵尸行被跳过)
    assert not ok and "'dep_pipe' failed today" in reason and "still running" not in reason


def test_normal_running_under_2x_unchanged():
    """正常 running (age < x2) → 行为不变, still running。"""
    db, orch = _orchestrator()
    _insert_run(db, "alive", "running", _ago(30))
    ok, reason = orch.can_run("factor_update")
    assert not ok and "still running" in reason
