"""持有期状态机测试 — 交易日到期/派生态/复评分流/负向对照"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from execution_layer.tables import (  # 正典 DDL: 夹具不再自己手写一份
    ACTION_RECORDS_DDL, STOCK_POOL_DDL)

from strategy_layer.lifecycle.holding import (
    HoldingStateMachine, STATE_AWAITING_REVIEW,
    effective_state, trading_days_between,
)
from strategy_layer.lifecycle.heat import (
    HeatAssessment, SignalResult, REC_EXIT_REVIEW, REC_HOLD, REC_INSUFFICIENT,
)


class _DBAdapter:
    """把裸 sqlite3.Connection 适配成 Database 接口 (仅 get_connection)。"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn

    def execute(self, *a, **k):
        return self._conn.execute(*a, **k)

    def commit(self):
        return self._conn.commit()


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "holding_test.db"))
    conn.row_factory = sqlite3.Row
    # 正典 DDL —— 原注释说"与生产列子集一致: 状态机只读写这些列", 这个前提
    # 已被 AUD-027 证伪 (状态机复活时要清 15 个了结字段, 窄表里大半不存在)。
    # 「够用的子集」会随代码演进失效, 单一定义点不会。
    conn.executescript(STOCK_POOL_DDL)
    conn.executescript(ACTION_RECORDS_DDL)
    conn.executescript("""
        CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL);
    """)
    yield _DBAdapter(conn)
    conn.close()


def _seed_calendar(db, days):
    """days = ['2026-08-17', ...] 交易日列表 → daily_klines 有记录即交易日。"""
    for d in days:
        db.execute("INSERT INTO daily_klines VALUES ('CAL',?,10.0)", (d,))
    db.commit()


def _mk_pool(db):
    from execution_layer.core.stock_pool import StockPool
    return StockPool(db)


class TestTradingDaysBetween:
    def test_counts_trading_days_only(self, db):
        """F-37 口径: 周末不算。08-21(五)→08-24(一) 持有 1 个交易日。"""
        _seed_calendar(db, ["2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25"])
        assert trading_days_between(db, "2026-08-21", "2026-08-24") == 1
        assert trading_days_between(db, "2026-08-21", "2026-08-25") == 2

    def test_start_day_not_counted(self, db):
        _seed_calendar(db, ["2026-08-20", "2026-08-21"])
        assert trading_days_between(db, "2026-08-20", "2026-08-20") == 0


class TestEffectiveState:
    def _row(self, status="active", added="2026-08-14", entry=None):
        return {"code": "300001", "status": status, "added_date": added,
                "entry_date": entry, "valid_until": None}

    def test_active_before_threshold_stays_active(self, db):
        # 08-14 后: 17,18,19 = 3 个交易日 < 5 → 未到期
        _seed_calendar(db, ["2026-08-17", "2026-08-18", "2026-08-19"])
        eff, held = effective_state(db, self._row(), "2026-08-19", min_hold_days=5)
        assert eff == "active" and held == 3

    def test_active_reaches_threshold_becomes_awaiting_review(self, db):
        days = [f"2026-08-{d}" for d in range(17, 22)]
        _seed_calendar(db, days)  # 08-14 后第 5 个交易日 = 08-21
        eff, held = effective_state(db, self._row(), "2026-08-21", min_hold_days=5)
        assert eff == STATE_AWAITING_REVIEW and held == 5

    def test_pending_entry_also_awaits(self, db):
        days = [f"2026-08-{d}" for d in range(17, 22)]
        _seed_calendar(db, days)
        eff, held = effective_state(db, self._row(status="pending_entry"),
                                    "2026-08-21", min_hold_days=5)
        assert eff == STATE_AWAITING_REVIEW

    def test_watch_passthrough(self, db):
        eff, held = effective_state(db, self._row(status="watch"), "2026-08-25")
        assert eff == "watch" and held is None

    def test_no_anchor_marks_undated(self, db):
        row = {"code": "X", "status": "active", "added_date": None,
               "entry_date": None, "valid_until": None}
        eff, _ = effective_state(db, row, "2026-08-25")
        assert eff.endswith(":undated")

    def test_calendar_gap_skips_not_zero(self, db):
        """锚点在日历末尾之后 → 区间无交易日 → held=None 且透传 (铁律 #12)。
        (注意 TradingCalendar 空库会退化工作日推算, 不能用空表测; 用真断档。)"""
        _seed_calendar(db, ["2026-08-10", "2026-08-11"])  # 日历止于 08-11
        row = self._row(added="2026-08-14")               # 锚点在日历覆盖之外
        eff, held = effective_state(db, row, "2026-09-01")
        assert eff == "active" and held is None


class FakeAssessor:
    """可编程热度评估器 — 状态机测试不依赖真信号查询。"""

    def __init__(self, rec=REC_HOLD):
        self.rec = rec
        self.calls = []

    def __call__(self, db, code, as_of):
        self.calls.append((code, as_of))
        sigs = {"s1": SignalResult(name="s1")}
        a = HeatAssessment(code=code, as_of=as_of, signals=sigs,
                           decay_count=2 if self.rec == REC_EXIT_REVIEW else 0,
                           support_count=1 if self.rec == REC_HOLD else 0,
                           recommendation=self.rec)
        a.summary = lambda: f"fake:{self.rec}"
        a.to_dict = lambda: {"rec": self.rec}
        return a


class TestStateMachineScan:
    def test_scan_flags_due_row(self, db):
        days = [f"2026-08-{d}" for d in range(17, 22)] + ["2026-08-24"]
        _seed_calendar(db, days)
        db.execute("INSERT INTO stock_pool (code,name,status,added_date) "
                   "VALUES ('300001','X','active','2026-08-14')")
        db.commit()
        sm = HoldingStateMachine(db)
        ds = sm.scan("2026-08-24")
        due = [d for d in ds if d.effective_state == STATE_AWAITING_REVIEW]
        assert len(due) == 1 and due[0].action == "review"

    def test_scan_overdue_beyond_max(self, db):
        # 08-14 后 11 个交易日 (> max 10) 才构成超期
        days = [f"2026-08-{d}" for d in range(17, 22)] + \
               ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
                "2026-08-31"]
        _seed_calendar(db, days)
        db.execute("INSERT INTO stock_pool (code,name,status,added_date) "
                   "VALUES ('300001','X','pending_entry','2026-08-14')")
        db.commit()
        sm = HoldingStateMachine(db, min_hold_days=5, max_hold_days=10)
        ds = sm.scan("2026-08-31")
        due = [d for d in ds if d.effective_state == STATE_AWAITING_REVIEW]
        assert due and due[0].action == "trigger_exit" and due[0].detail["overdue"]

    def test_scan_is_readonly(self, db):
        """dry-run 安全: scan 不写任何表。"""
        days = [f"2026-08-{d}" for d in range(17, 22)] + ["2026-08-24"]
        _seed_calendar(db, days)
        db.execute("INSERT INTO stock_pool (code,name,status,added_date) "
                   "VALUES ('300001','X','active','2026-08-14')")
        db.commit()
        n_actions = db.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
        sm = HoldingStateMachine(db)
        sm.scan("2026-08-24")
        n_after = db.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
        assert n_actions == n_after


class TestRunReview:
    def _setup(self, db, rec):
        days = [f"2026-08-{d}" for d in range(17, 22)]
        _seed_calendar(db, days)
        db.execute("INSERT INTO stock_pool (code,name,status,added_date) "
                   "VALUES ('300001','X','active','2026-08-14')")
        db.execute("INSERT INTO daily_klines VALUES ('300001','2026-08-21',66.0)")
        db.commit()
        pool = _mk_pool(db)
        assessor = FakeAssessor(rec)
        sm = HoldingStateMachine(db, stock_pool=pool, heat_assessor=assessor,
                                 min_hold_days=5, max_hold_days=10)
        decision = sm.scan("2026-08-21")[0]
        assert decision.effective_state == STATE_AWAITING_REVIEW
        return sm, decision, db

    def test_hold_extends_valid_until_and_logs(self, db):
        sm, decision, adb = self._setup(db, REC_HOLD)
        out = sm.run_review(decision, "2026-08-21")
        assert "extend_hold" in out["actions"]
        row = adb.execute(
            "SELECT valid_until FROM stock_pool WHERE code='300001'").fetchone()
        assert row["valid_until"] is not None  # 白名单扩展后写入成功
        log = adb.execute(
            "SELECT COUNT(*) FROM action_records WHERE code='300001' "
            "AND action_type='hold'").fetchone()[0]
        assert log >= 1

    def test_exit_review_triggers_exit_for_active(self, db):
        """复评不过 + active → exit_pending (走 mark_exit_trigger, 结算入口不变)。"""
        sm, decision, adb = self._setup(db, REC_EXIT_REVIEW)
        out = sm.run_review(decision, "2026-08-21")
        status = adb.execute(
            "SELECT status FROM stock_pool WHERE code='300001'").fetchone()[0]
        assert status == "exit_pending"
        assert any("trigger_exit" in a for a in out["actions"])

    def test_exit_review_cancels_pending_entry(self, db):
        """负向对照关键例: pending_entry 复评不过 → 回 watch, 不是 exit_pending。
        (mark_exit_trigger 只认 active, 硬调会静默 False — 正是本设计防的坑。)"""
        days = [f"2026-08-{d}" for d in range(17, 22)]
        _seed_calendar(db, days)
        db.execute("INSERT INTO stock_pool (code,name,status,added_date) "
                   "VALUES ('688326','经纬恒润','pending_entry','2026-08-14')")
        db.commit()
        pool = _mk_pool(db)
        sm = HoldingStateMachine(db, stock_pool=pool,
                                 heat_assessor=FakeAssessor(REC_EXIT_REVIEW),
                                 min_hold_days=5, max_hold_days=10)
        decisions = sm.scan("2026-08-21")
        dec = next(d for d in decisions if d.code == "688326")
        out = sm.run_review(dec, "2026-08-21")
        status = db.execute(
            "SELECT status FROM stock_pool WHERE code='688326'").fetchone()[0]
        assert status == "watch"
        assert "cancel_entry" in out["actions"]

    def test_insufficient_signal_neutral_hold_with_note(self, db):
        sm, decision, adb = self._setup(db, REC_INSUFFICIENT)
        out = sm.run_review(decision, "2026-08-21")
        assert "extend_hold" in out["actions"]
        reason = adb.execute(
            "SELECT reason FROM action_records WHERE code='300001' AND action_type='hold' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "信号不足" in reason

    def test_overdue_forces_exit_even_on_hold_rec(self, db):
        """超期未决 (> max_hold_days) 即使热度建议 hold 也走退出流程。"""
        sm, decision, adb = self._setup(db, REC_HOLD)
        decision.detail["overdue"] = True
        out = sm.run_review(decision, "2026-08-21")
        status = adb.execute(
            "SELECT status FROM stock_pool WHERE code='300001'").fetchone()[0]
        assert status == "exit_pending"

    def test_run_executes_all_due_and_isolates_errors(self, db):
        sm, _, _ = self._setup(db, REC_HOLD)
        report = sm.run("2026-08-21", execute=True)
        assert report["scanned"] >= 1
        assert report["executed"] and "error" not in report["executed"][0]

    def test_run_dry_mode_writes_nothing(self, db):
        sm, _, adb = self._setup(db, REC_HOLD)
        before = adb.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
        sm.run("2026-08-21", execute=False)
        after = adb.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
        assert before == after
