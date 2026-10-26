# -*- coding: utf-8 -*-
"""TDD RED: skipped=holiday 必须穿透 freshness_audit 消费层
(卡组二刀1 跨接线断链修复, main=c25129ff)

病灶 (判例#150 族「守卫没跨接线」): readiness.check_collect_freshness
正确返回 {"fresh": None, "skipped": "holiday"} (刀1 已并 main), 但
freshness_audit.run() 消费循环 `fresh = bool(info.get("fresh"))` —
bool(None)=False → _write_row(actual=None, has_data=False) → status=
'missing' 照写 → _alert_missing 照告警。假日守卫被消费层一笔抹掉,
下一个假日 (10-24 六) 16:22 审计 = 翻车现场。

修法: 消费循环识别 skipped, 落 status='skipped' 显形行 (不入 missing),
告警链只查 'missing' 天然豁免 (freshness_audit.py:166/:315 已是
status='missing' 过滤)。「假日缺」不是「无信息」— 显形不静默 (铁律#12)。
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database


def _db_with_artifact_tables():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock_financials (
            code TEXT, date TEXT, announce_date TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS stock_valuation (
            code TEXT, date TEXT, pe REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS limit_break_pool (
            code TEXT, date TEXT, name TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS stock_anomaly_reasons (
            code TEXT, date TEXT, tag_name TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS auction_benchmark (
            code TEXT, date TEXT, auction_pct REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS auction_snapshot (
            code TEXT, date TEXT, auction_price REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS hot_stock_history (
            code TEXT, date TEXT, rank INTEGER, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS skyrocket_list (
            code TEXT, date TEXT, rank INTEGER, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS limit_up_ladder (
            code TEXT, snapshot_date TEXT, board_level TEXT, PRIMARY KEY(code, snapshot_date, board_level));
        CREATE TABLE IF NOT EXISTS sector_fund_flow (
            name TEXT, date TEXT, sector_type TEXT, PRIMARY KEY(name, date));
    """)
    conn.commit()
    return db, conn


HOLIDAY = "2026-10-04"   # 国庆假日 (TradingCalendar._holidays 10-01~10-08)
TRADING = "2026-10-09"   # 节后首交易日


class TestSkippedPropagatesToAuditRow:
    def test_holiday_artifact_row_written_as_skipped(self):
        """假日工件表: freshness_audit 行 status='skipped', 不是 missing。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=HOLIDAY, db=db))
        rows = conn.execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name=?",
            (HOLIDAY, "limit_break_pool")).fetchall()
        assert rows, "假日工件表必须有 audit 行 (显形, 不静默缺席)"
        assert rows[0][0] == "skipped", (
            f"假日工件表 audit 行必须 status=skipped; 实得 {rows[0][0]}")

    def test_holiday_artifact_not_in_missing(self):
        """skipped 行不得被 _alert_missing / reconcile 当 missing 统计。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=HOLIDAY, db=db))
        n = conn.execute(
            "SELECT COUNT(*) FROM freshness_audit "
            "WHERE date=? AND status='missing'", (HOLIDAY,)).fetchone()[0]
        # 假日: hot/skyrocket/ladder/sector 自然日源当日无行是真 missing
        # (夹具未种数据), 但四张工件表一个都不许在 missing 里。
        m = conn.execute(
            "SELECT table_name FROM freshness_audit "
            "WHERE date=? AND status='missing'", (HOLIDAY,)).fetchall()
        tables = {r[0] for r in m}
        for t in ("limit_break_pool", "stock_anomaly_reasons",
                  "auction_benchmark", "auction_snapshot"):
            assert t not in tables, f"假日工件表 {t} 不得进 missing (假报根除)"

    def test_holiday_natural_day_missing_still_missing(self):
        """假日自然日源无行 → 仍 missing (守卫只豁免工件类, 不扩大)。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=HOLIDAY, db=db))
        r = conn.execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name=?",
            (HOLIDAY, "hot_stock_history")).fetchone()
        assert r and r[0] == "missing", (
            f"假日 hot 无行必须仍 missing; 实得 {r}")

    def test_trading_day_artifact_zero_rows_still_missing(self):
        """交易日工件表 0 行 → 仍 missing (10-08 实证真缺, 守卫不许吞)。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=TRADING, db=db))
        r = conn.execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name=?",
            (TRADING, "limit_break_pool")).fetchone()
        assert r and r[0] == "missing", (
            f"交易日 break 0 行必须 missing; 实得 {r}")

    def test_trading_day_with_rows_ok(self):
        """交易日工件表有行 → ok (守卫不碰正常路径)。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        conn.execute(
            "INSERT INTO limit_break_pool (code, date, name) VALUES ('600519', ?, 'x')",
            (TRADING,))
        conn.commit()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=TRADING, db=db))
        r = conn.execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name=?",
            (TRADING, "limit_break_pool")).fetchone()
        assert r and r[0] == "ok", f"交易日有行必须 ok; 实得 {r}"


class TestSkippedAlertReconcile:
    def test_skipped_rows_excluded_from_reconcile(self):
        """reconcile_missing_alerts: skipped 不当 missing 要求告警。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=HOLIDAY, db=db))
        from data_layer.pipelines.freshness_audit import reconcile_missing_alerts
        rep = reconcile_missing_alerts(conn, HOLIDAY)
        assert "limit_break_pool" not in (rep.get("tables") or []), (
            "reconcile 不得把 skipped 表当 missing (N4 甲-1 对账守卫)")

    def test_skipped_row_has_no_delay(self):
        """skipped 行 delay_min 记 None (无滞后语义), 不编 0 也不编天数。"""
        from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
        from data_layer.pipelines.base import PipelineInput
        db, conn = _db_with_artifact_tables()
        FreshnessAuditPipeline(db=db).run(PipelineInput(date=HOLIDAY, db=db))
        r = conn.execute(
            "SELECT delay_min FROM freshness_audit WHERE date=? AND table_name=?",
            (HOLIDAY, "limit_break_pool")).fetchone()
        assert r and r[0] is None, f"skipped 行 delay 必须 None; 实得 {r}"
