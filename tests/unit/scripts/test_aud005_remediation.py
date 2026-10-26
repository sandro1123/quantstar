"""AUD-005 存量处置脚本回归 — 解冻(④) + 回填(⑤) 的计划计算核心。

纪律 (照 D1-b 清创卡先例): 计划计算 (纯读) 与写入 (--apply) 分离,
dry 出全量 before/after 对拍账供人核对后才允许真写。
真写面归窗官; 本测试只验计划计算正确。
"""
import pytest

from common.database import Database
from scripts.aud005_remediation import (
    affected_stats_dates,
    compute_backfill_plan,
    compute_tracking_fix_plan,
    compute_unfreeze_plan,
)


TRADING_DAYS = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


@pytest.fixture
def db():
    d = Database(":memory:")
    conn = d.get_connection()
    conn.executescript("""
        CREATE TABLE stock_pool (
            code TEXT PRIMARY KEY, name TEXT, status TEXT, added_date TEXT,
            entry_price REAL, position_qty INTEGER, last_price TEXT,
            exit_price REAL, exit_date TEXT, exit_reason TEXT,
            total_return_pct REAL, total_fees REAL, net_return REAL,
            net_return_pct REAL, holding_days INTEGER,
            trigger_date TEXT, trigger_price REAL, updated_at TEXT
        );
        CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL,
                                   PRIMARY KEY (code, date));
    """)
    for d_ in TRADING_DAYS:
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES ('MKT', ?, 1.0)",
                     (d_,))
    conn.commit()
    yield d
    d.close()


def _kline(db, code, date, close):
    db.get_connection().execute(
        "INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
        (code, date, close))
    db.get_connection().commit()


class TestBackfillPlan:
    """⑤ 18 笔伪造账回填: exit_price==entry_price 且 ret=0.0 的行, 按 exit_date 真收盘价重算。"""

    def _fabricated(self, db, code="600001", entry=10.0, qty=100, exit_date="2026-09-08"):
        db.get_connection().execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty, "
            "exit_price, exit_date, exit_reason, total_return_pct) "
            "VALUES (?, 't', 'exited', ?, ?, ?, ?, 'T+1确认卖出', 0.0)",
            (code, entry, qty, entry, exit_date))
        db.get_connection().commit()

    def test_plan_recomputes_from_real_close(self, db):
        self._fabricated(db, "600001", entry=10.0, qty=100, exit_date="2026-09-08")
        _kline(db, "600001", "2026-09-08", 11.0)

        plan = compute_backfill_plan(db.get_connection())
        assert len(plan) == 1
        row = plan[0]
        assert row["code"] == "600001"
        assert row["old_return_pct"] == 0.0
        assert row["new_exit_price"] == 11.0
        assert row["new_return_pct"] == pytest.approx(10.0)     # (11-10)/10*100
        assert row["resolvable"] is True

    def test_healthy_rows_not_touched(self, db):
        db.get_connection().execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty, "
            "exit_price, exit_date, exit_reason, total_return_pct) "
            "VALUES ('600002','t','exited',10.0,100,12.0,'2026-09-08','T+1确认卖出',20.0)")
        db.get_connection().commit()
        _kline(db, "600002", "2026-09-08", 12.0)

        assert compute_backfill_plan(db.get_connection()) == [], (
            "只改伪造行 (ret=0.0), 健康行不得入计划 —— 幂等前提")

    def test_unresolvable_is_flagged_not_silently_skipped(self, db):
        """无当日 K 线的行必须显式标 resolvable=False, 不得静默漏掉。"""
        self._fabricated(db, "600003", exit_date="2026-09-08")
        # 故意不给 600003 建 K 线
        plan = compute_backfill_plan(db.get_connection())
        assert len(plan) == 1
        assert plan[0]["resolvable"] is False
        assert plan[0]["new_exit_price"] is None


class TestUnfreezePlan:
    """④ 22 只解冻: 按各自触发日的 T+1 交易日真收盘价回溯结算。"""

    def _pending(self, db, code, entry, updated_at, trigger_date=None):
        db.get_connection().execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty, "
            "trigger_date, updated_at) VALUES (?, 't', 'exit_pending', ?, 100, ?, ?)",
            (code, entry, trigger_date, updated_at + " 16:16:00"))
        db.get_connection().commit()

    def test_uses_real_trigger_date_when_present(self, db):
        self._pending(db, "600010", 10.0, "2026-09-09", trigger_date="2026-09-08")
        _kline(db, "600010", "2026-09-09", 10.5)

        plan = compute_unfreeze_plan(db.get_connection())
        assert len(plan) == 1
        r = plan[0]
        assert r["trigger_date"] == "2026-09-08"
        assert r["date_source"] == "trigger_date", "有真触发日就不得用代理"
        assert r["settle_date"] == "2026-09-09"
        assert r["settle_close"] == 10.5
        assert r["new_return_pct"] == pytest.approx(5.0)

    def test_falls_back_to_updated_at_proxy_and_says_so(self, db):
        """存量 22 只 trigger_date 全 NULL —— 代理口径必须显式标注, 不得隐藏。"""
        self._pending(db, "600011", 10.0, "2026-09-08", trigger_date=None)
        _kline(db, "600011", "2026-09-09", 9.0)

        r = compute_unfreeze_plan(db.get_connection())[0]
        assert r["trigger_date"] == "2026-09-08"
        assert r["date_source"] == "updated_at_proxy", (
            "用 updated_at 当触发日是代理口径, 必须在账上显形供人判断")
        assert r["settle_date"] == "2026-09-09"
        assert r["new_return_pct"] == pytest.approx(-10.0)

    def test_t1_not_yet_arrived_is_held_not_settled(self, db):
        """触发日的 T+1 尚未发生 -> 不结算, 交给修好的管道自然处理。"""
        self._pending(db, "600012", 10.0, "2026-09-11", trigger_date=None)

        r = compute_unfreeze_plan(db.get_connection())[0]
        assert r["settle_date"] is None
        assert r["action"] == "hold"
        assert "T+1" in r["note"]

    def test_missing_t1_close_is_flagged(self, db):
        """T+1 交易日已到但该股无 K 线 -> 标 unresolvable, 不得回落任何价。"""
        self._pending(db, "600013", 10.0, "2026-09-08", trigger_date=None)
        # 不给 600013 建 09-09 K 线
        r = compute_unfreeze_plan(db.get_connection())[0]
        assert r["action"] == "unresolvable"
        assert r["settle_close"] is None


class TestDownstreamFixes:
    """污染面比 stock_pool 大: post_exit_tracking 以伪造价为基线, execution_stats 由其派生。"""

    def test_tracking_baseline_recomputed_from_real_close(self, db):
        conn = db.get_connection()
        conn.executescript("""
            CREATE TABLE post_exit_tracking (
                code TEXT, exit_date TEXT, exit_price REAL, entry_price REAL,
                total_return_pct REAL, exit_reason TEXT, check_point TEXT,
                quality TEXT, created_at TEXT
            );
        """)
        conn.execute(
            "INSERT INTO stock_pool (code,name,status,entry_price,position_qty,"
            "exit_price,exit_date,exit_reason,total_return_pct) "
            "VALUES ('600001','t','exited',10.0,100,10.0,'2026-09-08','T+1确认卖出',0.0)")
        for cp in ("T+5", "T+10", "T+20"):
            conn.execute(
                "INSERT INTO post_exit_tracking (code, exit_date, exit_price, entry_price,"
                " total_return_pct, check_point, created_at) "
                "VALUES ('600001','2026-09-08',10.0,10.0,0.0,?,'2026-09-08 16:00:00')", (cp,))
        conn.commit()
        _kline(db, "600001", "2026-09-08", 11.0)

        bf = compute_backfill_plan(conn)
        fix = compute_tracking_fix_plan(conn, bf)
        assert len(fix) == 3, "三个检查点的基线都要重算"
        assert all(f["new_exit_price"] == 11.0 for f in fix)
        assert all(f["new_return_pct"] == pytest.approx(10.0) for f in fix)

    def test_affected_stats_dates_cover_both_plans(self, db):
        bf = [{"exit_date": "2026-09-07", "resolvable": True},
              {"exit_date": "2026-09-08", "resolvable": True}]
        uf = [{"action": "settle", "settle_date": "2026-09-09"},
              {"action": "hold", "settle_date": None}]
        assert affected_stats_dates(bf, uf) == ["2026-09-07", "2026-09-08", "2026-09-09"], (
            "回填日与解冻结算日都会改动当日胜负计数, 两边都要重算; hold 的不计入")
