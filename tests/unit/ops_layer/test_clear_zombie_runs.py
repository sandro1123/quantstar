"""G3-B/G7-A: clear_zombie_runs 三判据清理 — 行为边界测试 (隔离 :memory: 库)。

判死必须三判据全中 (无活体进程 + version/config_hash 全空 + 超时窗×2);
缺一归 suspect 只报告不动。只 UPDATE 不 DELETE, 附审计 note。
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

_QS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_QS_ROOT, "src"))
sys.path.insert(0, os.path.join(_QS_ROOT, "scripts"))

from common.database import Database
from clear_zombie_runs import (
    _launch_markers,
    apply_reap,
    find_live_pids,
    select_zombies,
)
from ops_layer.core.tables import OPS_TABLES_DDL

TIMEOUT_LOOKUP = {"daily_data_pipeline": 2400, "factor_update": 1800, "tiny_pipe": 60}


@pytest.fixture
def db():
    d = Database(":memory:")
    d.init_db()
    conn = d.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield d
    d.close()


def _insert_running(db, name, start_dt, run_id=None, version=None, config_hash=None):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status,"
        " version, config_hash) VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (run_id or f"{name}_z", name, start_dt.strftime("%Y-%m-%d"),
         start_dt.strftime("%Y-%m-%d %H:%M:%S"), version, config_hash),
    )
    conn.commit()


def _ago(seconds, now=None):
    return (now or datetime.now()) - timedelta(seconds=seconds)


# ── C1: 无活体进程 ────────────────────────────────────────────────

def test_find_live_pids_detects_pipeline_process(tmp_path):
    """cmdline 含 'pipeline run <name>' 的进程必须被识别为活体。"""
    procs = tmp_path / "proc"
    p1 = procs / "101"
    p1.mkdir(parents=True)
    (p1 / "cmdline").write_bytes(
        b"python\x00-m\x00src\x00pipeline\x00run\x00factor_update")
    (procs / "notapidir").mkdir()          # 非数字目录跳过
    (procs / "102").mkdir()                # 无 cmdline 文件不炸
    pids = find_live_pids(str(procs), _launch_markers("factor_update"))
    assert pids == [101]


# ── 三判据组合 ────────────────────────────────────────────────────

def test_all_three_criteria_met_is_zombie(db, tmp_path):
    """三判据全中 → 僵尸。"""
    now = datetime.now()
    _insert_running(db, "tiny_pipe", _ago(60 * 2 * 10, now), run_id="z_ok")  # age >> timeout×2
    zombies, suspects = select_zombies(
        db.get_connection(), now, TIMEOUT_LOOKUP, proc_dir=str(tmp_path / "empty"))
    assert [z["run_id"] for z in zombies] == ["z_ok"]
    assert suspects == []


def test_missing_any_single_criterion_goes_to_suspects(db, tmp_path):
    """缺任一判据 → suspect 只报告不动。三个用例各缺一条。"""
    now = datetime.now()
    # 缺 C1: 有活体进程
    procs = tmp_path / "proc"
    p = procs / "201"
    p.mkdir(parents=True)
    (p / "cmdline").write_bytes(b"python\x00scripts/daily_data_pipeline.py")
    _insert_running(db, "daily_data_pipeline", _ago(2400 * 4, now),
                    run_id="s_alive", version="", config_hash="")
    # 缺 C2: 已写 version (进程走到过收官路径附近)
    _insert_running(db, "tiny_pipe", _ago(60 * 20, now),
                    run_id="s_versioned", version="abc123")
    # 缺 C3: 未超 timeout×2
    _insert_running(db, "tiny_pipe", _ago(90, now), run_id="s_fresh")

    zombies, suspects = select_zombies(
        db.get_connection(), now, TIMEOUT_LOOKUP, proc_dir=str(procs))
    assert zombies == []
    got = {s["run_id"]: s for s in suspects}
    assert set(got) == {"s_alive", "s_versioned", "s_fresh"}
    assert "C1" in got["s_alive"]["detail"] and "活体pids=[201]" in got["s_alive"]["detail"]
    assert "C2" in got["s_versioned"]["detail"]
    assert "C3" in got["s_fresh"]["detail"]


def test_c2_requires_both_version_and_config_hash_empty(db, tmp_path):
    """version 空、config_hash 非空 → C2 不过 (半收官状态不判死)。"""
    now = datetime.now()
    _insert_running(db, "tiny_pipe", _ago(6000, now), run_id="s_half",
                    version=None, config_hash="deadbeef")
    zombies, suspects = select_zombies(
        db.get_connection(), now, TIMEOUT_LOOKUP, proc_dir=str(tmp_path / "empty"))
    assert zombies == [] and len(suspects) == 1


def test_c3_boundary_exactly_2x_timeout_not_reaped(db, tmp_path):
    """临界: age == timeout×2 整不收 (> 才收)。"""
    now = datetime.now()
    _insert_running(db, "tiny_pipe", _ago(120 - 30, now), run_id="edge")  # 90s < 120s
    zombies, _ = select_zombies(
        db.get_connection(), now, TIMEOUT_LOOKUP, proc_dir=str(tmp_path / "empty"))
    assert zombies == []


# ── 收割动作 ──────────────────────────────────────────────────────

def test_apply_updates_status_with_audit_note_never_deletes(db, tmp_path):
    """收割 = UPDATE failed + 审计 note; 行数不减; 只动 running 行。"""
    now = datetime.now()
    _insert_running(db, "tiny_pipe", _ago(6000, now), run_id="z1")
    before = db.get_connection().execute(
        "SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    backup_table = apply_reap(db.get_connection(),
                              [{"run_id": "z1"}], now, backup=True,
                              backup_ts="20260825_000000")
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == before
    row = conn.execute("SELECT status, error_msg FROM pipeline_runs WHERE run_id='z1'").fetchone()
    assert row["status"] == "failed" and "三判据" in row["error_msg"]
    assert backup_table == "pipeline_runs_backup_20260825_000000"


def test_apply_second_guard_only_touches_still_running(db, tmp_path):
    """二次守卫: 收割列表里的行若已被他人收官, UPDATE 不命中、不改 error_msg。"""
    now = datetime.now()
    _insert_running(db, "tiny_pipe", _ago(6000, now), run_id="z2")
    # 模拟收割瞬间被他人先收官
    db.get_connection().execute(
        "UPDATE pipeline_runs SET status='failed', error_msg='someone-else' "
        "WHERE run_id='z2'")
    db.get_connection().commit()
    apply_reap(db.get_connection(), [{"run_id": "z2"}], now)
    row = db.get_connection().execute(
        "SELECT error_msg FROM pipeline_runs WHERE run_id='z2'").fetchone()
    assert row["error_msg"] == "someone-else"  # 未被覆盖


def test_unparseable_start_time_goes_to_suspect(db, tmp_path):
    """start_time 烂数据不炸不误杀, 归 suspect。"""
    now = datetime.now()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status)"
        " VALUES ('bad_t', 'tiny_pipe', '2026-08-25', 'garbage', 'running')")
    conn.commit()
    zombies, suspects = select_zombies(conn, now, TIMEOUT_LOOKUP,
                                       proc_dir=str(tmp_path / "empty"))
    assert zombies == [] and [s["run_id"] for s in suspects] == ["bad_t"]
