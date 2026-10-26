"""ST-14: run_daily 拆分为阶段函数 — 单阶段可重跑 + 全链路行为一致 (对拍守护)

验收: run_daily 无 >80 行单体方法; 单阶段可独立调用 (如只重跑 select);
全链路结果与拆分前一致; STAGE_SPECS requires/produces 与阶段方法一致。
"""
import inspect
from unittest.mock import MagicMock, patch

from strategy_layer import (
    STAGE_SPECS,
    StrategyLayer,
    StrategyStageContext,
)
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.signals.generator import TradeSignal


def _make_layer():
    layer = StrategyLayer()
    layer.data_layer = MagicMock()
    layer.data_layer.get_risky_codes.return_value = set()
    return layer


def _run_full(layer, factor_daily, signals=None):
    with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
        with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
            with patch.object(layer.aggregator, "aggregate", return_value=factor_daily):
                with patch.object(layer.aggregator, "fill_recent_returns"):
                    with patch.object(layer, "market_regime", return_value="neutral"):
                        with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                            reg_cls.return_value.list_versions.return_value = []
                            with patch.object(layer.selector, "run",
                                              return_value=MagicMock(candidates=[])):
                                with patch.object(layer.deep_review, "review_candidates",
                                                  return_value=[]):
                                    with patch.object(layer.signal_generator, "generate",
                                                      return_value=signals or []):
                                        return layer.run_daily("2024-01-02")


class TestStageIndependence:
    def test_select_stage_rerunnable_alone(self):
        """验收: 单阶段可独立调用 — 只重跑 select (给定已算好的 factor_daily)"""
        layer = _make_layer()
        fd = FactorDaily(code="000001.SZ", date="2024-01-02", composite_score=1.0)
        ctx = StrategyStageContext(date="2024-01-02", factor_daily=[fd], regime="neutral")
        with patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])) as mock_run:
            aborted = layer._stage_select(ctx)
        assert aborted is False
        assert ctx.selection is not None
        mock_run.assert_called_once()

    def test_select_stage_fail_closed_returns_abort(self):
        layer = _make_layer()
        layer.data_layer.get_risky_codes.return_value = None  # fail-closed
        ctx = StrategyStageContext(date="2024-01-02", factor_daily=[], regime="neutral")
        with patch.object(layer.selector, "run") as mock_run:
            assert layer._stage_select(ctx) is True
        mock_run.assert_not_called()

    def test_agent_gate_stage_rerunnable_alone(self):
        layer = _make_layer()
        sel = MagicMock(candidates=[])
        ctx = StrategyStageContext(date="2024-01-02", selection=sel)
        layer._stage_agent_gate(ctx)
        assert ctx.agent_gate_enabled is True
        assert ctx.agent_reviews == []

    def test_signal_gen_stage_rerunnable_alone(self):
        layer = _make_layer()
        sel = MagicMock(candidates=[])
        sig = TradeSignal(code="000001.SZ", date="2024-01-02", action="buy")
        ctx = StrategyStageContext(date="2024-01-02", selection=sel, factor_daily=[])
        with patch.object(layer.signal_generator, "generate", return_value=[sig]):
            layer._stage_signal_gen(ctx)
        assert ctx.signals == [sig]


class TestFullPipelineConsistency:
    def test_full_run_produces_same_shape_as_before(self):
        """对拍: 全链路结果结构与拆分前一致 (date/signals/selection/regime/counts)"""
        layer = _make_layer()
        fd = FactorDaily(code="000001.SZ", date="2024-01-02", composite_score=1.0)
        sig = TradeSignal(code="000001.SZ", date="2024-01-02", action="buy")
        result = _run_full(layer, [fd], signals=[sig])
        assert result.date == "2024-01-02"
        assert result.signals == [sig]
        assert result.selection is not None
        assert result.regime == "neutral"
        assert result.stock_count == 1

    def test_fail_closed_aborts_with_empty_signals(self):
        layer = _make_layer()
        layer.data_layer.get_risky_codes.return_value = None
        result = _run_full(layer, [])
        assert result.signals == []
        assert result.selection is None

    def test_stages_called_in_order(self):
        layer = _make_layer()
        order = []
        for name in ("_stage_factor_compute", "_stage_aggregate", "_stage_ml_score",
                     "_stage_select", "_stage_agent_gate", "_stage_signal_gen"):
            orig = getattr(layer, name)
            def wrap(o=orig, n=name):
                def fn(ctx):
                    order.append(n)
                    return o(ctx)
                return fn
            setattr(layer, name, wrap())
        _run_full(layer, [])
        assert order == ["_stage_factor_compute", "_stage_aggregate", "_stage_ml_score",
                         "_stage_select", "_stage_agent_gate", "_stage_signal_gen"]


class TestStageSpecs:
    def test_every_stage_has_method(self):
        layer = StrategyLayer()
        for spec in STAGE_SPECS:
            assert hasattr(layer, f"_{spec['name']}") or hasattr(layer, f"_stage_{spec['name']}")

    def test_specs_cover_six_stages(self):
        names = [s["name"] for s in STAGE_SPECS]
        assert names == ["factor_compute", "aggregate", "ml_score",
                         "select", "agent_gate", "signal_gen"]

    def test_run_daily_no_long_method(self):
        """验收: run_daily 不再是 >80 行单体 (薄编排层)"""
        src = inspect.getsource(StrategyLayer.run_daily)
        assert len(src.splitlines()) <= 60
