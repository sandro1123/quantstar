"""ST-13: provenance 决策链溯源接线 — 每条信号落一条可回溯的决策快照

验收: 生产路径有 provenance 写入 (run_daily 信号生成后); 按 code+date 可反查完整决策链。
测试用临时库隔离 (生产库建表需用户 GO, 见修复日志)。
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

from common.database import Database
from strategy_layer import StrategyLayer
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.provenance import ProvenanceTracker, SignalProvenanceRecord
from strategy_layer.signals.generator import TradeSignal


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path), path


class TestProvenanceTracker:
    def test_creates_signal_provenance_table_and_roundtrip(self):
        db, path = _tmp_db()
        try:
            tracker = ProvenanceTracker(db=db)
            rec = SignalProvenanceRecord(
                code="000001.SZ", date="2024-01-02", action="buy", signal_id="sig1",
                composite_score=1.5, ml_score=0.3, regime="neutral",
                model_version="neutral_v1", weights_hash="abcd1234",
                factor_contributions={"momentum_20d": 0.4, "pe_ttm": -0.2},
                agent_confidence=0.8, agent_reason="无重大风险",
            )
            tracker.record_signal_provenance(rec)
            rows = tracker.get_signal_provenance("000001.SZ", "2024-01-02")
            assert len(rows) == 1
            r = rows[0]
            assert r["action"] == "buy"
            assert r["signal_id"] == "sig1"
            assert r["model_version"] == "neutral_v1"
            assert r["weights_hash"] == "abcd1234"
            assert r["factor_contributions"] == {"momentum_20d": 0.4, "pe_ttm": -0.2}
            assert r["agent_confidence"] == 0.8
        finally:
            os.unlink(path)

    def test_query_by_code_date_isolates(self):
        db, path = _tmp_db()
        try:
            tracker = ProvenanceTracker(db=db)
            tracker.record_signal_provenance(SignalProvenanceRecord(
                code="A", date="2024-01-02", action="buy", signal_id="s1"))
            tracker.record_signal_provenance(SignalProvenanceRecord(
                code="B", date="2024-01-02", action="buy", signal_id="s2"))
            assert len(tracker.get_signal_provenance("A", "2024-01-02")) == 1
            assert tracker.get_signal_provenance("A", "2024-01-03") == []
        finally:
            os.unlink(path)


class TestRunDailyProvenanceWiring:
    def test_run_daily_writes_provenance_for_each_signal(self):
        """验收核心: run_daily 生产路径为每条信号写决策链, 且可按 code+date 反查"""
        db, path = _tmp_db()
        try:
            layer = StrategyLayer()
            layer.db = db
            layer.config = {"strategy.provenance.enabled": True}  # 写库需用户 GO, 测试显式启用
            layer.data_layer = MagicMock()
            layer.data_layer.get_risky_codes.return_value = set()

            fd = FactorDaily(code="000001.SZ", date="2024-01-02",
                             composite_score=1.0, factor_scores={"momentum_20d": 2.0})
            sig = TradeSignal(code="000001.SZ", date="2024-01-02",
                              action="buy", signal_id="sigX")
            with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
                with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                    with patch.object(layer.aggregator, "aggregate", return_value=[fd]):
                        with patch.object(layer.aggregator, "fill_recent_returns"):
                            with patch.object(layer, "market_regime", return_value="neutral"):
                                with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                                    reg_cls.return_value.list_versions.return_value = []
                                    with patch.object(layer.selector, "run",
                                                      return_value=MagicMock(candidates=[])):
                                        with patch.object(layer.deep_review, "review_candidates",
                                                          return_value=[]):
                                            with patch.object(layer.signal_generator, "generate",
                                                              return_value=[sig]):
                                                layer.run_daily("2024-01-02")
            tracker = ProvenanceTracker(db=db)
            rows = tracker.get_signal_provenance("000001.SZ", "2024-01-02")
            assert len(rows) == 1
            assert rows[0]["action"] == "buy"
            assert rows[0]["signal_id"] == "sigX"
            assert rows[0]["composite_score"] == 1.0
            assert rows[0]["regime"] == "neutral"
        finally:
            os.unlink(path)

    def test_provenance_failure_does_not_block_signals(self):
        """provenance 写入失败不阻塞信号产出"""
        layer = StrategyLayer()
        layer.db = MagicMock()
        layer.db.get_connection.side_effect = Exception("db down")
        layer.config = {"strategy.provenance.enabled": True}  # 显式启用以触发失败路径
        layer.data_layer = MagicMock()
        layer.data_layer.get_risky_codes.return_value = set()
        fd = FactorDaily(code="000001.SZ", date="2024-01-02", composite_score=1.0)
        sig = TradeSignal(code="000001.SZ", date="2024-01-02", action="buy")
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer.aggregator, "aggregate", return_value=[fd]):
                    with patch.object(layer.aggregator, "fill_recent_returns"):
                        with patch.object(layer, "market_regime", return_value="neutral"):
                            with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                                reg_cls.return_value.list_versions.return_value = []
                                with patch.object(layer.selector, "run",
                                                  return_value=MagicMock(candidates=[])):
                                    with patch.object(layer.deep_review, "review_candidates",
                                                      return_value=[]):
                                        with patch.object(layer.signal_generator, "generate",
                                                          return_value=[sig]):
                                            result = layer.run_daily("2024-01-02")
        assert len(result.signals) == 1
