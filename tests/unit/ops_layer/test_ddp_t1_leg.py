# -*- coding: utf-8 -*-
"""B4: DDP T+1 \u817f\u8c6d\u514d (2026-10-21)\u3002

\u77db\u76fe: DDP \u6309\u5f53\u65e5\u5224 (records==0/partial \u2192 degraded \u2192 \u7236 partial),
freshness_audit \u6309\u4e0a\u4e00\u4ea4\u6613\u65e5\u5224 (readiness.T1_COLLECT_TABLES)\u3002
lhb_daily/index_daily \u662f T+1 \u6e90 (\u6b21\u65e5 ~08:03 \u843d\u5e93), DDP 15:30 \u8dd1\u65f6\u5f53\u65e5
\u5fc5\u7136\u672a\u53d1\u5e03 \u2192 \u5929\u5929 partial (10-20 \u5b9e\u8bc1)\u3002

\u4fee\u6cd5\u8fb9\u754c: \u53ea\u8c6d\u514d T1_COLLECT_TABLES \u4e24\u817f\u5728 DDP best_effort \u94fe\u5185\u7684\u964d\u7ea7;
\u5176\u4f59 9 \u817f\u4e0d\u52a8; :785 \u53cc\u817f\u76f4\u8c03 handler \u4e0d\u52a8 (\u4fee\u6cd5\u6ce8\u91ca\u5199\u660e)\u3002
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

DATE = "2026-10-20"
PREV = "2026-10-19"
T1_LEGS = ("lhb_daily", "index_daily")


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def _seed(db, prev_rows):
    """kline \u8986\u76d6\u7387\u95e8\u7981\u7528 5000 \u884c + \u4e0a\u4e00\u4ea4\u6613\u65e5\u951a; T+1 \u817f prev \u65e5\u884c\u6570\u7531\u53c2\u6570\u5851\u5f62\u3002"""
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    conn.executemany(
        "INSERT INTO daily_klines(code, date, close) VALUES (?, ?, 10.0)",
        [(str(i).zfill(6), DATE) for i in range(5000)] + [("000001", PREV)],
    )
    conn.execute("CREATE TABLE IF NOT EXISTS lhb_daily (code TEXT, date TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS index_daily (code TEXT, date TEXT)")
    for table, n in prev_rows.items():
        conn.executemany(
            "INSERT INTO %s(code, date) VALUES (?, ?)" % table,
            [(str(i).zfill(6), PREV) for i in range(n)],
        )
    conn.commit()


def _fake():
    def run_pipeline(name, pipeline_input):
        if name in T1_LEGS:
            # 10-20 \u751f\u4ea7\u5f62\u6001: \u817f partial (\u5f53\u65e5\u672a\u53d1\u5e03), index \u8fd8\u5199\u4e86\u5386\u53f2\u56de\u586b\u884c
            return PipelineResult(
                name=name, date=pipeline_input.date, status="partial",
                records_written=10,
                errors=["%s: \u5f53\u65e5\u672a\u53d1\u5e03 (date=%s)" % (name, pipeline_input.date)],
            )
        return PipelineResult(
            name=name, date=pipeline_input.date,
            status="success", records_written=10, errors=[],
        )
    return run_pipeline


def _run_ddp(db, orchestrator, monkeypatch=None):
    orchestrator._pipeline_configs = {
        "daily_data_pipeline": {"max_retries": 0, "timeout_s": 30,
                                "start_date": DATE, "end_date": DATE},
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)
    if monkeypatch is not None:
        # \u6a21\u62df\u201c\u8c6d\u514d\u88ab\u632a\u6389\u201d \u2014\u2014 patch \u6a21\u5757\u5168\u5c40\u51fd\u6570
        # (\u751f\u4ea7\u4ee3\u7801\u8c03\u7684\u662f\u6a21\u5757\u5168\u5c40, patch \u5b9e\u4f8b\u5c5e\u6027\u591f\u4e0d\u7740\u4f1a\u5047\u7eff)
        monkeypatch.setattr("ops_layer.orchestrator._t1_leg_on_track",
                            lambda *a, **k: False)
    data_layer = SimpleNamespace(
        db=db, run_pipeline=_fake(),
        pipelines={"shortterm_collect": object(), "index_daily": object()},
    )
    orchestrator.set_app(SimpleNamespace(data=data_layer))
    orchestrator._executor.shutdown(wait=True)
    orchestrator._executor = ThreadPoolExecutor(max_workers=1)
    return orchestrator.run_pipeline("daily_data_pipeline")


def test_t1_legs_today_missing_yesterday_present_is_ok():
    """\u6b63\u5411\u9489: T1 \u817f\u5f53\u65e5\u7f3a\u4f46\u6628\u65e5\u6709 \u2192 \u7236\u904e ok \u4e0d\u7b97 partial\u3002"""
    db, orchestrator = _orchestrator()
    _seed(db, {"lhb_daily": 30, "index_daily": 5})
    result = _run_ddp(db, orchestrator)
    assert result.status == "success", "T+1 \u817f\u5f53\u65e5\u672a\u53d1\u5e03\u4e0d\u5e94\u62d6\u6210 partial: %s" % (result.error,)
    assert "lhb_daily" not in (result.error or "")
    assert "index_daily" not in (result.error or "")
    db.close()


def test_exemption_removed_falls_back_to_partial_with_leg_names():
    """\u53cd\u5411\u9489: \u632a\u6389\u8c6d\u514d \u2192 \u540c\u4e00 fixture \u5fc5 partial \u4e14 error \u542b\u817f\u540d\u3002"""
    db, orchestrator = _orchestrator()
    _seed(db, {"lhb_daily": 30, "index_daily": 5})
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    try:
        result = _run_ddp(db, orchestrator, monkeypatch=mp)
    finally:
        mp.undo()
    assert result.status == "partial"
    assert "lhb_daily" in (result.error or "")
    assert "index_daily" in (result.error or "")
    db.close()


def test_t1_legs_missing_yesterday_too_stays_partial():
    """\u7b2c\u4e09\u9489: T1 \u817f\u8fde\u6628\u65e5\u4e5f\u6ca1\u6709 \u2192 \u4ecd\u5224 partial (\u8c6d\u514d\u4e0d\u653e\u5bbd)\u3002"""
    db, orchestrator = _orchestrator()
    _seed(db, {"lhb_daily": 0, "index_daily": 0})
    result = _run_ddp(db, orchestrator)
    assert result.status == "partial", "\u8fde\u6628\u65e5\u4e5f\u65e0\u884c\u5fc5\u987b\u4ecd\u964d\u7ea7"
    assert "lhb_daily" in (result.error or "")
    assert "index_daily" in (result.error or "")
    db.close()
