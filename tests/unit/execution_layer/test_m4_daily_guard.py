# -*- coding: utf-8 -*-
"""REC 通电卡 · 每日守护腿测试 (接线3)。

统筹接线3: consistency_check + coverage 需每日调度腿 (否则「每日自问」无载体)。
腿 = execution_layer/tracking/daily_guard.run_daily_guard + orchestrator handler
+ ops.yaml 排程项 (crontab 由统筹 22:05 窗亲挂)。
"""
import pytest

from common.database import Database


def _db():
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    ensure_schema(db)
    return db


def test_daily_guard_runs_and_writes_coverage():
    """守护腿跑通并把覆盖率落库 (metric 落库=卡要求 5)。"""
    from execution_layer.tracking.daily_guard import run_daily_guard, COVERAGE_DDL
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-24", rationale_id="r1",
            verdict="推荐", reason="量价齐升")
    result = run_daily_guard(db, asof="2026-09-25")
    assert result["tracked_total"] == 1
    # F-22 (2026-10-21) 修正: 原断言 == 0.0, 但 0.0 恰恰**没能**让缺口显形
    # (09-28~10-20 共 23 天 ratio 恒 0.0, 无人发现 record_advice 生产零调用)。
    # 分子为 0 = 根本没测, 应写 NULL + measured=False 才是真·缺口显形。
    assert result["coverage_ratio"] is None, "无建议留痕 → 未测得(NULL), 不是覆盖 0"
    assert result["measured"] is False
    conn = db.get_connection()
    row = conn.execute(
        "SELECT tracked_total, coverage_ratio FROM recommendation_tracking_coverage "
        "WHERE date='2026-09-25'").fetchone()
    assert row is not None, "覆盖率 metric 未落库"
    assert row[0] == 1


def test_daily_guard_detects_silent_flip_on_stop_loss_break():
    """跌破止损 → still_valid=False → 无建议留痕则 silent_flip 显形。"""
    from execution_layer.tracking.daily_guard import run_daily_guard
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db = _db()
    conn = db.get_connection()
    # 建最小依赖表 (rationale + klines + tracking with stop_loss)
    from agent_layer.rationale import RATIONALE_DDL
    conn.executescript(RATIONALE_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    conn.execute("INSERT INTO recommendation_rationale "
                 "(rationale_id, code, rec_date, invalidation_conditions) "
                 "VALUES ('r1','600001','2026-09-24','[\"跌破止损\"]')")
    conn.execute("INSERT INTO daily_klines VALUES ('600001','2026-09-25',9.0)")
    # 止损在入池侧 stock_pool (tracking 只读引用不复制持仓语义)
    conn.execute("CREATE TABLE IF NOT EXISTS stock_pool (code TEXT PRIMARY KEY, "
                 "name TEXT, status TEXT, stop_loss REAL, entry_date TEXT)")
    conn.execute("INSERT INTO stock_pool (code, name, status, stop_loss) "
                 "VALUES ('600001','测试A','active',9.5)")
    conn.commit()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-24", rationale_id="r1",
            verdict="推荐", reason="x")
    result = run_daily_guard(db, asof="2026-09-25")
    assert result["silent_flip_count"] == 1, \
        "跌破止损且无建议留痕 → 无声翻脸必须显形"


def test_daily_guard_no_tracking_is_honest_zero():
    """零跟踪记录 → 诚实零 (不造假; census_guard 判例)。"""
    from execution_layer.tracking.daily_guard import run_daily_guard
    db = _db()
    result = run_daily_guard(db, asof="2026-09-25")
    assert result["tracked_total"] == 0
    assert result["coverage_ratio"] is None, "零跟踪覆盖率须 None 不造 0%"


def test_daily_leg_registered_in_orchestrator():
    """接线3: handler + 分发注册在位。"""
    import inspect
    from ops_layer import orchestrator
    src = inspect.getsource(orchestrator)
    assert "_run_rec_tracking_daily" in src, "handler 未实现"
    assert '"rec_tracking_daily": self._run_rec_tracking_daily' in src, "分发未注册"


def test_daily_leg_scheduled_in_ops_yaml():
    """接线3: ops.yaml 有排程项 (crontab 由统筹 22:05 窗亲挂)。"""
    import os
    p = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "config", "defaults", "ops.yaml")
    src = open(os.path.abspath(p), encoding="utf-8").read()
    assert "rec_tracking_daily:" in src, "ops.yaml 缺 rec_tracking_daily 排程项"
    i = src.find("rec_tracking_daily:")
    seg = src[i:i + 400]
    assert "schedule:" in seg and "depends_on:" in seg
    assert "retrospect_settlement" in seg, "须挂 retrospect_settlement 同族"
