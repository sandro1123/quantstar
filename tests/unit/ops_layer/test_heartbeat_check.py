"""P0-3①: 心跳模型 — 超期未成功主动告警 (纯逻辑核心 + 接线 + CLI)"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.heartbeat_check import (
    DEFAULT_GRACE_S, budget_s, check, check_and_alert)

NOW = "2026-08-27"


def _dt(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.strptime(f"{NOW} {h}:{m}", "%Y-%m-%d %H:%M")


def _cfg(schedule="16:00", timeout_s=120, retries=0, deps=None):
    return {"schedule": schedule, "timeout_s": timeout_s,
            "max_retries": retries, "depends_on": deps or []}


def _seed(db, name, status, date=NOW):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{name}-{status}-{date}", name, date, f"{date} 16:00:00", status))
    conn.commit()


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


# -- 纯逻辑核心 ---------------------------------------------------------------

def test_within_grace_not_overdue():
    """未到期 (排程+预算+宽限内) → 不打扰"""
    pipes = {"factor_update": _cfg()}  # deadline = 16:00 + 2min + 30min = 16:32
    assert check(pipes, [], _dt("16:20")) == []


def test_past_deadline_no_success_is_overdue():
    """超期且当日无成功行 → overdue (心跳缺失)"""
    pipes = {"factor_update": _cfg()}
    out = check(pipes, [], _dt("17:00"))
    assert len(out) == 1
    assert out[0]["name"] == "factor_update"
    assert out[0]["overdue_min"] > 0


def test_success_row_is_heartbeat():
    """当日 success 行 = 心跳打点 → 不 overdue"""
    pipes = {"factor_update": _cfg()}
    runs = [{"pipeline_name": "factor_update", "status": "success"}]
    assert check(pipes, runs, _dt("17:00")) == []


def test_partial_counts_as_heartbeat():
    """partial (数据已落库降级) 同样算心跳 — 与熔断器口径一致"""
    pipes = {"factor_update": _cfg()}
    runs = [{"pipeline_name": "factor_update", "status": "partial"}]
    assert check(pipes, runs, _dt("17:00")) == []


def test_failed_row_still_overdue():
    """failed 行不算心跳 — 失败了≠完成了, 仍属"应到而未到" """
    pipes = {"factor_update": _cfg()}
    runs = [{"pipeline_name": "factor_update", "status": "failed"}]
    assert len(check(pipes, runs, _dt("17:00"))) == 1


def test_continuous_schedule_not_monitored():
    """continuous/无时刻表 → 无"应到"概念, 不监控"""
    pipes = {"intraday": _cfg(schedule="continuous")}
    assert check(pipes, [], _dt("17:00")) == []


def test_grace_override_per_pipeline():
    """hb_cfg.overrides 每管道宽限覆盖 (长管道防误报)"""
    pipes = {"ddp": _cfg(schedule="15:30", timeout_s=2700)}
    # 默认宽限 30min: 17:00 已超期 (deadline 16:47)
    assert len(check(pipes, [], _dt("17:00"))) == 1
    # 覆盖宽限 3600s: deadline 17:17, 17:00 未到期
    assert check(pipes, [], _dt("17:00"),
                 {"overrides": {"ddp": 3600}}) == []


def test_budget_formula_matches_watchdog():
    """预算公式 = cron_watchdog.sh DL-65 同源: 重试天花板 + 依赖等待窗"""
    # 重试: 120 x 3 + 30 x 2 = 420
    assert budget_s(_cfg(retries=2)) == 420
    # 依赖: max(120, 900+60) = 960
    assert budget_s(_cfg(deps=["x"])) == 960


def test_yesterday_success_not_heartbeat(db):
    """昨日成功不是今日心跳 — check_and_alert 按当日过滤"""
    _seed(db, "factor_update", "success", date="2026-08-26")
    pipes = {"factor_update": _cfg()}
    out = check_and_alert(db, pipes, now=_dt("17:00"))
    assert len(out) == 1


# -- 接线: 告警状态机联动 -------------------------------------------------------

def test_overdue_creates_alert(db):
    """超期 → alerts 落库 (标题含管道名, resolve_pipeline_alerts 可匹配)"""
    pipes = {"factor_update": _cfg()}
    check_and_alert(db, pipes, now=_dt("17:00"))
    rows = db.get_connection().execute(
        "SELECT title FROM alerts WHERE status='open'").fetchall()
    assert len(rows) == 1
    assert "factor_update" in rows[0]["title"]


def test_repeat_check_no_duplicate_alert(db):
    """P0-3② 联动: 持续超期第二轮 → 状态机抑制, 不重复轰炸"""
    pipes = {"factor_update": _cfg()}
    check_and_alert(db, pipes, now=_dt("17:00"))
    check_and_alert(db, pipes, now=_dt("17:15"))
    count = db.get_connection().execute(
        "SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert count == 1


def test_recovery_resolves_heartbeat_alert(db):
    """补跑成功 → resolve_pipeline_alerts 自动关闭心跳告警 (闭环)"""
    from ops_layer.alert_manager import AlertManager
    pipes = {"factor_update": _cfg()}
    check_and_alert(db, pipes, now=_dt("17:00"))
    n = AlertManager(db).resolve_pipeline_alerts("factor_update")
    assert n == 1
    open_count = db.get_connection().execute(
        "SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
    assert open_count == 0


# -- CLI -----------------------------------------------------------------------

def test_cli_exit_codes(tmp_path):
    """CLI: 无超期 rc=0; 有超期 rc=1; 配置缺失 rc=2"""
    root = Path(__file__).resolve().parents[3]
    env_db = str(tmp_path / "hb.db")
    yaml_path = tmp_path / "ops.yaml"
    # 排程 00:01 + 极小预算 → 任何时刻核对都超期
    yaml_path.write_text(
        "ops:\n  pipelines:\n    p1:\n      schedule: \"00:01\"\n"
        "      timeout_s: 10\n      max_retries: 0\n      depends_on: []\n",
        encoding="utf-8")
    db = Database(env_db)
    db.get_connection().executescript(OPS_TABLES_DDL)
    db.get_connection().commit()
    db.close()
    import os
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "QS_DB_PATH": env_db}
    py = sys.executable
    r1 = subprocess.run([py, "-m", "ops_layer.heartbeat_check",
                         "--yaml", str(yaml_path), "--now", "2026-01-05 12:00"],
                        capture_output=True, text=True, env=env, cwd=str(root))
    assert r1.returncode == 1, r1.stdout + r1.stderr
    assert "OVERDUE" in r1.stdout
    # 空管道表 → 无超期
    yaml_path.write_text("ops:\n  pipelines: {}\n", encoding="utf-8")
    r0 = subprocess.run([py, "-m", "ops_layer.heartbeat_check",
                         "--yaml", str(yaml_path)],
                        capture_output=True, text=True, env=env, cwd=str(root))
    assert r0.returncode == 0, r0.stdout + r0.stderr
    # 配置缺失 → rc=2
    r2 = subprocess.run([py, "-m", "ops_layer.heartbeat_check",
                         "--yaml", str(tmp_path / "nope.yaml")],
                        capture_output=True, text=True, env=env, cwd=str(root))
    assert r2.returncode == 2
