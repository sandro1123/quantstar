"""ddp 两小卡② — late_success 对账: pipeline_runs status vs 实际落库自检

红绿: 补跑链 success 足迹真实存在 → ok; 缺失 → mismatch (不拿 status 冒充
数据); 未登记映射 → unverified (不假 OK); 查询异常 → error (不静默吞错)。
08-26 一次性人工实证的可持续化 (0980e30 ddp 预算修复遗留卡)。
"""
import sqlite3

import pytest

from ops_layer.late_success_recon import (
    FOOTPRINTS, find_late_success, recon_one, run_recon)

SCHEMA = """
CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY, pipeline_name TEXT, date TEXT,
    status TEXT, created_at TEXT);
CREATE TABLE factor_daily (date TEXT, code TEXT, pe_ttm REAL);
CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL);
CREATE TABLE quality_checks (date TEXT, table_name TEXT, check_name TEXT,
    level TEXT, result TEXT, detail TEXT, created_at TEXT);
"""


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "recon.db")
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO factor_daily VALUES ('2026-08-26','000001',10.0)")
    # r5 (kline 08-27 补跑链 success) 的足迹 — 当日有行 → ok
    conn.execute("INSERT INTO daily_klines VALUES ('000001','2026-08-27',10.0)")
    conn.executemany(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?)", [
            # late_success: failed → success (补跑链形态)
            ("r1", "factor_update", "2026-08-26", "failed", "2026-08-26 16:05:00"),
            ("r2", "factor_update", "2026-08-26", "success", "2026-08-26 17:30:00"),
            # 纯 success (无坏前态) — 不是 late_success, 不入对账
            ("r3", "kline", "2026-08-26", "success", "2026-08-26 16:00:00"),
            # 坏终态在前 + 足迹在 daily_klines 有行
            ("r4", "kline", "2026-08-27", "skipped", "2026-08-27 16:01:00"),
            ("r5", "kline", "2026-08-27", "success", "2026-08-27 17:35:00"),
            # mismatch: success 但业务表无该日行
            ("r6", "quality_check", "2026-08-27", "failed", "2026-08-27 16:02:00"),
            ("r7", "quality_check", "2026-08-27", "success", "2026-08-27 17:36:00"),
            # unverified: 映射未登记的管道
            ("r8", "evening_report", "2026-08-27", "skipped", "2026-08-27 16:03:00"),
            ("r9", "evening_report", "2026-08-27", "success", "2026-08-27 17:37:00"),
        ])
    conn.commit()
    conn.close()
    return p


class TestFindLateSuccess:
    def test_priors_required(self, db):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        items = find_late_success(conn, "2026-08-26")
        # 只有 r2 (failed 在前); r3 纯 success 不入
        assert [i["run_id"] for i in items] == ["r2"]
        assert items[0]["prior_statuses"] == ["failed"]
        conn.close()

    def test_all_three_priors_counted(self, db):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        items = {i["run_id"]: i for i in find_late_success(conn, "2026-08-27")}
        assert set(items) == {"r5", "r7", "r9"}
        assert items["r5"]["prior_statuses"] == ["skipped"]
        conn.close()

    def test_pipeline_filter(self, db):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        items = find_late_success(conn, "2026-08-27", pipeline="kline")
        assert [i["run_id"] for i in items] == ["r5"]
        conn.close()


class TestReconOne:
    def test_footprint_ok(self, db):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = recon_one(conn, {"run_id": "r2", "pipeline_name": "factor_update",
                             "date": "2026-08-26", "created_at": "",
                             "prior_statuses": ["failed"]})
        assert r["verdict"] == "ok"
        assert r["footprint_rows"] == 1

    def test_footprint_mismatch_honest(self, db):
        """足迹缺失 → mismatch, 不拿 status=success 冒充数据落库"""
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = recon_one(conn, {"run_id": "r7", "pipeline_name": "quality_check",
                             "date": "2026-08-27", "created_at": "",
                             "prior_statuses": ["failed"]})
        assert r["verdict"] == "mismatch"
        assert "未落库" in r["reason_zh"]

    def test_unverified_not_fake_ok(self, db):
        """映射缺 → unverified, 明示未覆盖, 绝不静默判 OK"""
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = recon_one(conn, {"run_id": "r9", "pipeline_name": "evening_report",
                             "date": "2026-08-27", "created_at": "",
                             "prior_statuses": ["skipped"]})
        assert r["verdict"] == "unverified"

    def test_missing_table_error(self, tmp_path):
        """足迹表不存在 → error, 不静默吞错"""
        p = str(tmp_path / "bare.db")
        conn = sqlite3.connect(p)
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE quality_checks")
        conn.commit()
        conn.close()
        ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        r = recon_one(ro, {"run_id": "x", "pipeline_name": "quality_check",
                           "date": "2026-08-27", "created_at": "",
                           "prior_statuses": ["failed"]})
        assert r["verdict"] == "error"
        ro.close()


class TestRunRecon:
    def test_summary_counts_and_exit_semantics(self, db):
        r = run_recon(db, "2026-08-27")
        assert r["total"] == 3
        assert r["verdict_counts"] == {"ok": 1, "mismatch": 1, "unverified": 1}
        assert r["recon_ok"] is False  # mismatch 存在 → 对账不通过

    def test_clean_day_passes(self, db):
        r = run_recon(db, "2026-08-26")
        assert r["verdict_counts"] == {"ok": 1}
        assert r["recon_ok"] is True


class TestFootprintsRegistry:
    def test_all_footprints_two_fields(self):
        for name, fp in FOOTPRINTS.items():
            assert isinstance(fp, tuple) and len(fp) == 2, name
            assert fp[1], f"{name} 日期列不能为空"

    def test_daily_backtest_uses_real_column(self):
        """08-26 真数据教训: backtest_runs 无 run_date 列, 实际列 =
        date_range_start (08-28 唯一行实证) — 映射须跟真实 DDL 对齐"""
        assert FOOTPRINTS["daily_backtest"] == ("backtest_runs", "date_range_start")
