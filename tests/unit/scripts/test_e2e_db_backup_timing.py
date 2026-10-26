"""回归测试: e2e_verification db_backup 时序误报修复。

db_backup cron 在 23:00, e2e full 检查在 18:00 -> 当天无记录是正常的,
旧逻辑把它标 fail(误报)。修复: db_backup 改查最近 36h 内是否有成功运行。
"""
import sqlite3
from datetime import datetime, timedelta
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts"))
import e2e_verification as e2e


class FakeApp:
    def __init__(self, conn): self._conn = conn
    @property
    def db(self):
        class D:
            def get_connection(inner): return self._conn
        return D()


def _seed(conn, runs):
    """runs: list of (pipeline_name, date, status, duration_s, error_msg)"""
    conn.execute("CREATE TABLE pipeline_runs (pipeline_name TEXT, date TEXT, status TEXT, duration_s REAL, error_msg TEXT)")
    for r in runs:
        conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?,?)", r)
    conn.commit()


def test_db_backup_same_day_passes():
    """当天有 db_backup success 记录 -> 通过。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [("db_backup","2026-08-14","success",181.0,"")])
    app = FakeApp(conn)
    res = e2e.check_pipelines(app, "2026-08-14", ["db_backup"])
    assert res[0]["status"] == "pass", res


def test_db_backup_no_today_but_yesterday_pass():
    """当天无 db_backup 记录(18:00 检查时 23:00 还没跑), 但昨天有 success -> 应通过(修复核心)。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [("db_backup","2026-08-13","success",181.0,"")])
    app = FakeApp(conn)
    res = e2e.check_pipelines(app, "2026-08-14", ["db_backup"])
    assert res[0]["status"] == "pass", f"昨天有备份应通过, got {res}"


def test_db_backup_too_old_fails():
    """最近的 db_backup 超出窗口(>1天) -> fail。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [("db_backup","2026-08-10","success",136.0,"")])
    app = FakeApp(conn)
    res = e2e.check_pipelines(app, "2026-08-14", ["db_backup"])
    assert res[0]["status"] == "fail", f"5天前的备份应失败, got {res}"


def test_db_backup_latest_failed_fails():
    """最近 db_backup 是 failed 状态 -> fail(不因窗口存在就放行)。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [("db_backup","2026-08-13","failed",136.0,"handler returned None")])
    app = FakeApp(conn)
    res = e2e.check_pipelines(app, "2026-08-14", ["db_backup"])
    assert res[0]["status"] == "fail", f"最近备份failed应失败, got {res}"


def test_normal_pipeline_unchanged():
    """非 LATE 管道(如 end_of_day)当天无记录仍应 fail(行为不变)。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [])
    app = FakeApp(conn)
    res = e2e.check_pipelines(app, "2026-08-14", ["end_of_day"])
    assert res[0]["status"] == "fail", f"end_of_day 当天无记录应fail, got {res}"
