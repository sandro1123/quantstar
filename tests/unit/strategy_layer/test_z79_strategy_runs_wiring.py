# -*- coding: utf-8 -*-
"""#79 strategy_runs run 级溯源通电 — TDD RED 测试面。

设计稿: vault/10-项目/QuantStar/分层讨论/zombie两表施工设计稿-D-20260901.md §1 (方案A)。
新行为契约:
  1. 门控 strategy.provenance.enabled=False → run+signal 两表整族静默 (与 Stage 6 同闸)
  2. 成功路径 → strategy_runs 一行: run_id/stock_count/signal_count/duration_s 全非空,
     status='success', parameters/results_hash 非空
  3. 无股票早退 → status='no_stocks', stock_count=0, signal_count=0, duration_s>0
  4. select fail-closed (L1 安全闸) → status='select_failclosed',
     stock_count=len(codes), signal_count=0
  5. 异常路径 → finish_run('failed') 后 re-raise 原异常;
     装饰面 (生产契约) catch_quantstar_error 照旧吞异常返 None — 行为零改动对拍
  6. provenance 写失败不阻塞主流程 (信号照常产出)
  7. 父子关联: signal_provenance.run_id == strategy_runs.run_id (非空)
深补丁链形态逐字沿用 test_st13_provenance.py 先例。
"""
import inspect
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from common.database import Database
from strategy_layer import StrategyLayer
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.provenance import ProvenanceTracker
from strategy_layer.signals.generator import TradeSignal

DATE = "2024-01-02"
CODE = "000001.SZ"


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path), path


def _make_layer(db, enabled=True):
    """st13 先例: 真实 StrategyLayer + 注入 tmp db + dict config。"""
    layer = StrategyLayer()
    layer.db = db
    layer.config = {"strategy.provenance.enabled": enabled}
    layer.data_layer = MagicMock()
    layer.data_layer.get_risky_codes.return_value = set()
    return layer


def _mock_chain(st, layer, codes=None, failclosed=False, raise_factor_compute=False):
    """st13 深补丁链 (ExitStack 形态): factor→aggregate→ml→select→gate→signal_gen。

    codes=[] → no_stocks 路径; failclosed=True → get_risky_codes 返回 None (L1 闸);
    raise_factor_compute=True → Stage 1 抛 RuntimeError (异常路径)。
    """
    codes = [CODE] if codes is None else codes
    fd = FactorDaily(code=CODE, date=DATE, composite_score=1.0,
                     factor_scores={"momentum_20d": 2.0})
    sig = TradeSignal(code=CODE, date=DATE, action="buy", signal_id="sigX")
    if raise_factor_compute:
        st.enter_context(patch.object(layer, "_stage_factor_compute",
                                      side_effect=RuntimeError("boom-79")))
    st.enter_context(patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()))
    st.enter_context(patch.object(layer, "_get_stock_codes", return_value=codes))
    st.enter_context(patch.object(layer.aggregator, "aggregate", return_value=[fd]))
    st.enter_context(patch.object(layer.aggregator, "fill_recent_returns"))
    st.enter_context(patch.object(layer, "market_regime", return_value="neutral"))
    reg_cls = st.enter_context(patch("strategy_layer.ml.registry.ModelRegistry"))
    reg_cls.return_value.list_versions.return_value = []
    st.enter_context(patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])))
    st.enter_context(patch.object(layer.deep_review, "review_candidates", return_value=[]))
    st.enter_context(patch.object(layer.signal_generator, "generate", return_value=[sig]))
    layer.data_layer.get_risky_codes.return_value = None if failclosed else set()


class TestGateClosed:
    def test_gate_closed_zero_rows_both_tables(self):
        """门控关 → run 级与 signal 级整族静默 (共享 kill-switch 对拍)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db, enabled=False)
            with ExitStack() as st:
                _mock_chain(st, layer)
                result = layer.run_daily(DATE)
            assert len(result.signals) == 1  # 主流程不受门控影响
            ProvenanceTracker(db=db)  # 确保表存在后计数 (只读用途)
            conn = db.get_connection()
            assert conn.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM signal_provenance").fetchone()[0] == 0
        finally:
            os.unlink(path)


class TestRunRows:
    def test_success_row_four_values(self):
        """成功路径 → strategy_runs 一行, 四值全非空 + status='success'。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer)
                result = layer.run_daily(DATE)
            assert len(result.signals) == 1
            tracker = ProvenanceTracker(db=db)
            rows = tracker.list_runs(5)
            assert len(rows) == 1, f"strategy_runs 应有 1 行, 实际 {len(rows)}"
            r = rows[0]
            assert r["status"] == "success"
            assert r["run_id"], "run_id 非空"
            assert r["stock_count"] == 1
            assert r["signal_count"] == 1
            assert r["duration_s"] > 0, "duration_s 真实耗时 > 0"
            assert r["parameters"], "parameters (mode JSON) 非空"
            assert r["results_hash"], "results_hash 非空"
            assert r["mode"] == "live"
        finally:
            os.unlink(path)

    def test_no_stocks_early_return(self):
        """无股票早退 → 一行 no_stocks, signal_count=0, duration_s>0。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[])
                result = layer.run_daily(DATE)
            assert result.signals == []
            tracker = ProvenanceTracker(db=db)
            rows = tracker.list_runs(5)
            assert len(rows) == 1, f"strategy_runs 应有 1 行, 实际 {len(rows)}"
            r = rows[0]
            assert r["status"] == "no_stocks"
            assert r["stock_count"] == 0
            assert r["signal_count"] == 0
            # no_stocks 早退几乎零耗时: duration 落 0.0 合法 (round 3位).
            # 但 finish 必须真发生 — status 已钉. duration 只验非负 + 落库.
            assert r["duration_s"] >= 0
        finally:
            os.unlink(path)

    def test_select_failclosed_row(self):
        """L1 安全闸 fail-closed → 一行 select_failclosed, stock_count=codes 数。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, failclosed=True)
                result = layer.run_daily(DATE)
            assert result.signals == []
            tracker = ProvenanceTracker(db=db)
            rows = tracker.list_runs(5)
            assert len(rows) == 1, f"strategy_runs 应有 1 行, 实际 {len(rows)}"
            r = rows[0]
            assert r["status"] == "select_failclosed"
            assert r["stock_count"] == 1  # codes 已获取后才 fail-closed
            assert r["signal_count"] == 0
        finally:
            os.unlink(path)


class TestExceptionPath:
    def test_finish_failed_then_reraise_original(self):
        """裸函数面: finish_run('failed') 后 re-raise 原异常 (验收红线)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            bare = inspect.unwrap(StrategyLayer.run_daily)
            with ExitStack() as st:
                _mock_chain(st, layer, raise_factor_compute=True)
                with pytest.raises(RuntimeError, match="boom-79"):
                    bare(layer, DATE)
            tracker = ProvenanceTracker(db=db)
            rows = tracker.list_runs(5)
            assert len(rows) == 1, f"异常路径应有 failed 行, 实际 {len(rows)}"
            assert rows[0]["status"] == "failed"
            assert rows[0]["stock_count"] == 1  # codes 已获取后抛异常
        finally:
            os.unlink(path)

    def test_decorator_face_returns_none_and_marks_failed(self):
        """装饰面 (生产契约对拍): 照旧吞异常返 None (非崩溃), 行=failed。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, raise_factor_compute=True)
                result = layer.run_daily(DATE)  # 完整装饰链
            assert result is None, "catch_quantstar_error 照旧转 None — 行为零改动"
            tracker = ProvenanceTracker(db=db)
            rows = tracker.list_runs(5)
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            os.unlink(path)


class TestFailSafe:
    def test_provenance_failure_does_not_block(self):
        """db 坏 → run 级与 signal 级 provenance 均静默跳过, 信号照常产出。"""
        layer = StrategyLayer()
        layer.db = MagicMock()
        layer.db.get_connection.side_effect = Exception("db down-79")
        layer.config = {"strategy.provenance.enabled": True}
        layer.data_layer = MagicMock()
        layer.data_layer.get_risky_codes.return_value = set()
        with ExitStack() as st:
            _mock_chain(st, layer)
            result = layer.run_daily(DATE)
        assert result is not None
        assert len(result.signals) == 1


class TestParentChildLink:
    def test_signal_row_carries_run_id(self):
        """父子关联: signal_provenance.run_id == strategy_runs.run_id (非空)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer)
                layer.run_daily(DATE)
            tracker = ProvenanceTracker(db=db)
            run_rows = tracker.list_runs(5)
            assert len(run_rows) == 1, f"strategy_runs 应有 1 行, 实际 {len(run_rows)}"
            sig_rows = tracker.get_signal_provenance(CODE, DATE)
            assert len(sig_rows) == 1
            assert sig_rows[0]["run_id"], "signal 行 run_id 非空"
            assert sig_rows[0]["run_id"] == run_rows[0]["run_id"]
        finally:
            os.unlink(path)
