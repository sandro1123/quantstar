"""TDD: e2e db_backup 窗口按交易日伸缩 (fix/e2e-backup-tradingday-window)

派单 sm-3ea29070027b4ee4 第2件。根因: scripts/e2e_verification.py:51-52
LATE_PIPELINE_WINDOW_DAYS=1 自然日差 — 10-19(周一) 检查时最近备份
10-16(周五), 自然日差 3 > 1 → fail。但 10-17/18/19 周末+周日 cron 不跑
备份, 这是正常周末间隔。窗口对周末无感知。

修法 (统筹采纳「对因」不堆特例): 判据改按交易日 —
最近备份日到检查日之间的交易日数 ≤1 即 pass, 周末/假日不计。
复用 TradingCalendar (日历面, 不读 daily_klines — 库停更=病源,
拿它当锚=用有病产物当基线, 同 readiness prev_trading_day_before v4)。

统筹四钉:
① 正常交易日差 1 天 pass
② 周一检查上周五备份 pass (交易日差 1, 自然日差 3)
③ 真超窗 (差 ≥2 个交易日) fail
④ 假日连续不 fail
"""
import sqlite3
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
    conn.execute("CREATE TABLE pipeline_runs (pipeline_name TEXT, date TEXT, status TEXT, duration_s REAL, error_msg TEXT)")
    for r in runs:
        conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?,?)", r)
    conn.commit()

def _run(target_date, backup_date, status="success"):
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [("db_backup", backup_date, status, 181.0, "")])
    return e2e.check_pipelines(FakeApp(conn), target_date, ["db_backup"])

# ---- 统筹四钉 ----

def test_prev_trading_day_one_gap_passes():
    """钉①: 正常交易日差 1 天 (10-16 备份, 10-19 周一检查 — 交易日差1) pass。"""
    res = _run("2026-10-19", "2026-10-16")
    assert res[0]["status"] == "pass", f"周一查周五备份 (交易日差1) 必须 pass; got {res}"

def test_monday_after_weekend_passes():
    """钉②: 周末后首个交易日 — 自然日差 3 但交易日差 1, 必须 pass (10-19 事故复现)。"""
    # 10-17(周六)/10-18(周日)/10-19(周一) — 备份 10-16(周五)
    res = _run("2026-10-19", "2026-10-16")
    assert res[0]["status"] == "pass", f"周末间隔不得误报; got {res}"

def test_real_window_breach_fails():
    """钉③: 真超窗 (差 ≥2 个交易日) fail — 10-14 备份, 10-19 检查 (交易日差3)。"""
    res = _run("2026-10-19", "2026-10-14")
    assert res[0]["status"] == "fail", f"交易日差3必须 fail; got {res}"

def test_holiday_gap_not_counted():
    """钉④: 假日连续不计 — 备份 10-08 前最后交易日 09-30, 10-09 检查 pass。"""
    # 国庆 10-01~10-08 假日; 备份 09-30(周三, 节前最后交易日), 10-09(周五, 节后首日) 检查
    res = _run("2026-10-09", "2026-09-30")
    assert res[0]["status"] == "pass", f"国庆长假间隔 (交易日差1) 不得 fail; got {res}"

def test_holiday_gap_two_trading_days_fails():
    """钉④对偶: 长假+真超窗 (09-29 备份, 10-09 检查 = 交易日差2) fail。"""
    res = _run("2026-10-09", "2026-09-29")
    assert res[0]["status"] == "fail", f"交易日差2必须 fail (09-29→09-30→10-09); got {res}"

def test_failed_backup_still_fails():
    """回归 (既有测试同义): 最近备份 failed → fail, 不因窗口放行。"""
    res = _run("2026-10-19", "2026-10-16", status="failed")
    assert res[0]["status"] == "fail", f"failed 备份必须 fail; got {res}"

def test_no_run_record_still_fails():
    """零记录 → fail (哨兵不哑)。"""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _seed(conn, [])
    res = e2e.check_pipelines(FakeApp(conn), "2026-10-19", ["db_backup"])
    assert res[0]["status"] == "fail", f"无运行记录必须 fail; got {res}"

def test_message_mentions_trading_days_not_calendar():
    """报文按交易日表述 (运维判读不误解为自然日)。"""
    res = _run("2026-10-19", "2026-10-14")
    detail = res[0]["detail"]
    assert "交易日" in detail, f"超窗报文须按交易日表述; got {detail}"
