"""Tests for Backtest Layer models and WFO gate"""
from backtest_layer._models import BacktestMetrics, AnalysisReport, PositionConcentration
from backtest_layer.core.models import BacktestInput, BacktestReport, CostConfig
from backtest_layer.feedback.wfo_gate import WFOGate


class TestBacktestModels:
    def test_backtest_metrics_defaults(self):
        m = BacktestMetrics()
        assert m.sharpe == 0.0
        # BT-06: sqn/capture_ratio 默认 None = 不可算, 不再用 0.0 假值
        assert m.sqn is None
        assert m.capture_ratio is None

    def test_backtest_metrics_with_values(self):
        m = BacktestMetrics(sharpe=1.5, max_drawdown=-0.2, sqn=3.0)
        assert m.sharpe == 1.5
        assert m.sqn == 3.0

    def test_analysis_report_defaults(self):
        r = AnalysisReport()
        assert r.analyzers == {}

    def test_position_concentration_defaults(self):
        p = PositionConcentration()
        assert p.hhi == 0.0
        assert p.num_positions == 0

    def test_cost_config_defaults(self):
        c = CostConfig()
        assert c.initial_capital == 100000.0
        assert c.commission_rate == 0.00025

    def test_backtest_input_minimal(self):
        inp = BacktestInput(mode="hold", codes=["000001"], date_range=("2026-01-01", "2026-07-24"))
        assert inp.mode == "hold"
        assert inp.codes == ["000001"]
        assert isinstance(inp.cost, CostConfig)

    def test_backtest_report_required_fields(self):
        r = BacktestReport(mode="hold", date_range=("2026-01-01", "2026-07-24"),
                          total_return=0.1, annual_return=0.05, sharpe=0.5,
                          max_drawdown=-0.1, win_rate=0.6, total_trades=10,
                          benchmark_return=0.03, excess_return=0.02)
        assert r.mode == "hold"
        assert r.total_return == 0.1
        assert r.sharpe == 0.5


class TestWFOGate:
    def _ir(self, s=1.0):
        return {"sharpe": s, "max_drawdown": 0.10, "total_trades": 150}

    def _or(self, s=0.8, dd=0.10, t=150, e=0.05):
        return {"sharpe": s, "max_drawdown": dd, "total_trades": t, "excess_return": e}

    def test_passes_good_oos(self):
        gate = WFOGate(min_sharpe=0.5, min_trades=100)
        result = gate.evaluate(self._ir(1.0), self._or(0.8, t=120))
        assert result.passed is True

    def test_fails_low_oos_sharpe(self):
        gate = WFOGate(min_sharpe=0.5, min_trades=0, max_drawdown=0.50)
        result = gate.evaluate(self._ir(1.0), self._or(0.3))
        assert result.passed is False

    def test_fails_high_degradation(self):
        gate = WFOGate(min_sharpe=0.5, min_trades=0, max_drawdown=0.50)
        result = gate.evaluate(self._ir(1.0), self._or(0.4))
        assert result.passed is False
        assert result.degradation > 0.5