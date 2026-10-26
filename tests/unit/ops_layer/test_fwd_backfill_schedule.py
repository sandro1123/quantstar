"""DL-34: 前向收益每日回填调度 — orchestrator _run_fwd_return_backfill handler。

设计: 新建独立管道 fwd_return_backfill (depends_on factor_update), 每日为"刚兑现"的
日期回填 fwd_return_1d/5d/20d。复用 data_layer.backfill_fwd_returns (幂等, 仅写 NULL,
未兑现保持 NULL, 百分点单位)。本测试验证 handler 的调度行为, 不触生产库。

红-绿: 撤回 orchestrator handler 时本文件必须失败。
"""
from types import SimpleNamespace

import pytest

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator_with_kline_dates(dates):
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    for i, d in enumerate(dates):
        conn.execute("INSERT INTO daily_klines VALUES (?, ?, ?)", (f"00000{i % 10}", d, 10.0 + i))
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _fake_app(db, backfill_calls, backfill_return=None):
    def backfill_fwd_returns(target_date, horizons=(1, 5, 20)):
        backfill_calls.append((target_date, tuple(horizons)))
        return backfill_return if backfill_return is not None else {h: 100 for h in horizons}
    data = SimpleNamespace(db=db, backfill_fwd_returns=backfill_fwd_returns)
    return SimpleNamespace(data=data)


_DATES = [f"2026-08-{d:02d}" for d in range(1, 31)]  # 30 个交易日


def test_handler_backfills_recently_realized_dates(monkeypatch):
    db, orch = _orchestrator_with_kline_dates(_DATES)
    calls = []
    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, calls))

    result = orch._run_fwd_return_backfill({"catchup_days": 3})

    assert result.success is True
    # 每个 horizon 回填最近 3 个"已兑现"日期。latest=_DATES[-1]。
    # horizon h 的最近兑现日 = _DATES[-1-h]; catchup 3 → 再往前 2 个。
    by_horizon = {}
    for target, horizons in calls:
        for h in horizons:
            by_horizon.setdefault(h, []).append(target)
    for h in (1, 5, 20):
        expected = [_DATES[-1 - h - i] for i in range(3)]
        assert by_horizon.get(h) == expected, f"horizon {h}: {by_horizon.get(h)} != {expected}"
    db.close()


def test_handler_reports_filled_counts_in_metadata(monkeypatch):
    db, orch = _orchestrator_with_kline_dates(_DATES)
    calls = []
    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, calls, backfill_return={5: 42}))

    result = orch._run_fwd_return_backfill({"catchup_days": 1})

    assert result.success is True
    assert result.metadata.get("filled_total", 0) > 0
    db.close()


def test_handler_fails_observably_when_data_layer_missing(monkeypatch):
    db, orch = _orchestrator_with_kline_dates(_DATES)
    monkeypatch.setattr(orch, "_get_app", lambda: SimpleNamespace(data=None))

    result = orch._run_fwd_return_backfill({})

    # 铁律 #12: 不能静默成功; data 层缺失必须报失败。
    assert result.success is False
    assert result.errors
    db.close()


def test_handler_skips_horizon_when_history_too_short(monkeypatch):
    # 只有 3 个交易日 → horizon 5/20 无兑现日可回填, 不应调用也不应报错。
    db, orch = _orchestrator_with_kline_dates(_DATES[:3])
    calls = []
    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, calls))

    result = orch._run_fwd_return_backfill({"catchup_days": 3})

    assert result.success is True
    called_horizons = {h for _, hs in calls for h in hs}
    assert 5 not in called_horizons
    assert 20 not in called_horizons
    db.close()


def test_handler_swallows_per_date_error_but_reports(monkeypatch):
    db, orch = _orchestrator_with_kline_dates(_DATES)

    def flaky(target_date, horizons=(1, 5, 20)):
        if target_date == _DATES[-6]:  # horizon 5 的最近兑现日
            raise RuntimeError("locked")
        return {h: 1 for h in horizons}
    data = SimpleNamespace(db=db, backfill_fwd_returns=flaky)
    monkeypatch.setattr(orch, "_get_app", lambda: SimpleNamespace(data=data))

    result = orch._run_fwd_return_backfill({"catchup_days": 3})

    # 单日失败不拖垮整条管道 (best-effort), 但必须留痕。
    assert result.metadata.get("errors") or result.errors
    db.close()
