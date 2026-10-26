# -*- coding: utf-8 -*-
"""QS 卡 readiness-announce-aware-unify — T4 假日不红钉 (假日面文件) v3。

monkeypatch 面修正: health() 内部 `from common.utils import bj_date` (函数内
import, common.utils.bj_date 是 patch 对象 — patch common.utils.bj_date)。
"""
import re
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

# T6: 审计锚豁免名单 (判例 #159: 注理由+受管)
_HOLIDAY_AUDIT_DATES = {"2026-10-04"}  # 理由: 国庆非交易日, 假日判绿+标注验证


def test_t6_self_no_real_trading_dates():
    """反向钉 (判例 #159): 审计锚必须全为非交易日。"""
    assert _HOLIDAY_AUDIT_DATES, "审计锚空 = 钉失效 (判例 #150 自检形态)"
    from common.calendar import TradingCalendar
    cal = TradingCalendar()
    bad = [d for d in _HOLIDAY_AUDIT_DATES if cal.is_trading_day(d)]
    assert not bad, f"假日面审计锚混入真交易日 {bad} — 豁免混装 (判例 #159)"


def test_t6b_audit_dates_declared():
    """审计锚声明完整性: 锚格式合法。"""
    for d in _HOLIDAY_AUDIT_DATES:
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", d), f"审计锚 {d} 格式非法"


@pytest.fixture()
def fiscal_db_holiday():
    """假日面: 审计日=10-04 (monkeypatch common.utils.bj_date); 数据行任意。"""
    from common.database import Database
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE stock_financials (
            code TEXT NOT NULL, date TEXT NOT NULL,
            roe REAL DEFAULT 0, PRIMARY KEY (code, date))
    """)
    for i in range(10):
        conn.execute(
            "INSERT INTO stock_financials (code, date, roe) VALUES (?,?,?)",
            (f"60050{i}", "2026-06-30", 10.0 + i))
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT NOT NULL, date TEXT NOT NULL,
            close REAL, PRIMARY KEY (code, date))
    """)
    for i in range(20):
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES (?,?,?)",
            (f"60050{i}", "2026-09-30", 100.0))
    conn.commit()
    yield db
    conn.close()
    os.unlink(path)


def _run_health_holiday(db):
    """health() 内部 bj_date → patch common.utils.bj_date (函数内 import 面)。"""
    from data_layer import DataLayer
    import common.utils as _utils
    audit_d = next(iter(_HOLIDAY_AUDIT_DATES))
    dl = DataLayer.__new__(DataLayer)
    dl.db = db
    with mock.patch.object(_utils, "bj_date", return_value=audit_d):
        lh = dl.health()
    return lh


def test_t4_holiday_not_red_with_marker(fiscal_db_holiday):
    """T4: 非交易日 → health 全表 pass + detail 含 holiday 标注。"""
    lh = _run_health_holiday(fiscal_db_holiday)
    checks = {c.name: c for c in lh.checks}
    c = checks["freshness_stock_financials"]
    assert c.status == "pass", f"假日须不红: {c.status} — {c.detail}"
    assert "holiday (non-trading day)" in (c.detail or ""), (
        f"假日判绿必须留 holiday 标注 (可观测性硬要求): {c.detail}")


def test_t4c_holiday_marker_in_daily_tables_too(fiscal_db_holiday):
    """T4 扩: 日更表 (daily_klines) 在假日同样 pass + 标注 (非季表也豁免)。"""
    lh = _run_health_holiday(fiscal_db_holiday)
    checks = {c.name: c for c in lh.checks}
    c = checks["freshness_daily_klines"]
    # 09-30 距 10-04 = 4 天 > y=3 → 正常判 fail; 假日 → pass + 标注
    assert c.status == "pass", f"假日日更表须 pass: {c.status} — {c.detail}"
    assert "holiday" in (c.detail or ""), f"日更表假日标注缺失: {c.detail}"


def test_t4d_holiday_mark_all_tables(fiscal_db_holiday):
    """T4 全表扫描: 假日 audit 日所有非空表都须 pass+标注。

    例外 (观测性硬要求的双向): ①表无数据 fail (真断血假日也显形不豁免)
    ②表缺失 warn (freshness 检查失败 — 表不存在非假日问题, 夹具无该表)。
    """
    lh = _run_health_holiday(fiscal_db_holiday)
    for c in lh.checks:
        if c.name.startswith("freshness_") and c.status != "pass":
            detail = c.detail or ""
            assert ("表无数据" in detail or "检查失败" in detail), (
                f"假日判 {c.status} 但非空表: {c.name} — {detail}")
