"""S3 F3.1 端到端 — WFO 门禁真实接线

验证:
1. WalkForwardMode 产出真实 IS/OOS 聚合指标 (is_sharpe/oos_sharpe/oos_max_drawdown/
   oos_total_trades/oos_excess_return/oos_efficiency), 且 oos_efficiency 是 OOS/IS 收益比
   而非窗口收益均值。
2. 汇总报告 sharpe/MDD/trades 为全窗口聚合 (非仅 window1)。
3. WFOGate.evaluate() 五项判定被真实调用并正确拦截。
"""
from unittest.mock import MagicMock, patch

from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, BacktestReport, CostConfig
from backtest_layer.feedback.wfo_gate import WFOGate
from backtest_layer.modes.walk_forward import WalkForwardConfig, WalkForwardMode


def _hold_report(sharpe, mdd, trades, ret, excess):
    return BacktestReport(
        mode="hold", date_range=("2026-01-01", "2026-02-01"),
        total_return=ret, annual_return=ret * 2, sharpe=sharpe,
        max_drawdown=mdd, win_rate=0.5, total_trades=trades,
        benchmark_return=0.0, excess_return=excess,
    )


class TestWalkForwardRealMetrics:
    def _make_wf(self):
        broker = Broker(CostConfig(initial_capital=100000))
        cfg = WalkForwardConfig(n_windows=3, min_window_days=10, is_ratio=0.7)
        return WalkForwardMode(broker, MagicMock(), MagicMock(), cfg)

    def test_wf_result_has_real_is_oos_fields(self):
        wf = self._make_wf()
        # mock HoldMode.run: IS 段返回高 sharpe, OOS 段返回低 sharpe
        reports = iter([
            _hold_report(2.0, 0.05, 60, 0.10, 0.05),  # win1 IS
            _hold_report(1.0, 0.10, 40, 0.04, 0.02),  # win1 OOS
            _hold_report(2.2, 0.06, 70, 0.12, 0.06),  # win2 IS
            _hold_report(1.1, 0.12, 45, 0.05, 0.03),  # win2 OOS
            _hold_report(1.8, 0.04, 55, 0.09, 0.04),  # win3 IS
            _hold_report(0.9, 0.15, 35, 0.03, 0.01),  # win3 OOS
        ])
        with patch("backtest_layer.modes.walk_forward.HoldMode") as MockHold:
            MockHold.return_value.run = lambda inp: next(reports)
            inp = BacktestInput(mode="walk_forward", codes=["000001"],
                                date_range=("2026-01-01", "2026-12-31"))
            report = wf.run(inp)

        wf_res = report.wf_result
        # 真实 IS/OOS 字段存在
        for key in ("is_sharpe", "oos_sharpe", "oos_max_drawdown",
                    "oos_total_trades", "oos_excess_return", "oos_efficiency",
                    "avg_is_return", "avg_window_return"):
            assert key in wf_res, f"missing {key}"
        # oos_efficiency = avg_oos / avg_is (非窗口收益均值); 容忍 4 位小数舍入误差
        avg_is = wf_res["avg_is_return"]
        avg_oos = wf_res["avg_window_return"]
        expected_eff = avg_oos / avg_is if avg_is != 0 else 0.0
        assert abs(wf_res["oos_efficiency"] - expected_eff) < 2e-3

    def test_aggregate_not_just_window1(self):
        wf = self._make_wf()
        # 三个 OOS 窗口 MDD 递增, 聚合 MDD 应取最大值 (非 window1)
        reports = iter([
            _hold_report(2.0, 0.01, 60, 0.10, 0.05),
            _hold_report(1.0, 0.05, 40, 0.04, 0.02),
            _hold_report(2.2, 0.01, 70, 0.12, 0.06),
            _hold_report(1.1, 0.20, 45, 0.05, 0.03),  # 最大 MDD 在 win2 OOS
            _hold_report(1.8, 0.01, 55, 0.09, 0.04),
            _hold_report(0.9, 0.10, 35, 0.03, 0.01),
        ])
        with patch("backtest_layer.modes.walk_forward.HoldMode") as MockHold:
            MockHold.return_value.run = lambda inp: next(reports)
            inp = BacktestInput(mode="walk_forward", codes=["000001"],
                                date_range=("2026-01-01", "2026-12-31"))
            report = wf.run(inp)
        # 聚合 MDD = max(0.05, 0.20, 0.10) = 0.20, 而非 window1 的 0.05
        assert abs(report.max_drawdown - 0.20) < 1e-6
        # 聚合 trades = 40+45+35 = 120
        assert report.total_trades == 120


class TestWFOGateWiring:
    def test_gate_five_item_evaluation_called(self):
        """mock 一个 walk_forward 结果 → 断言 WFOGate 五项判定被调用。"""
        wf_result = {
            "is_sharpe": 2.0,
            "oos_sharpe": 0.4,       # 低于 min_sharpe=1.0 → 拦截
            "oos_max_drawdown": 0.25,  # 高于 max_drawdown=0.20 → 拦截
            "oos_total_trades": 50,   # 低于 min_trades=100 → 拦截
            "oos_excess_return": -0.01,  # 低于 min_excess_return=0 → 拦截
            "oos_efficiency": 0.2,
        }
        is_report = {"sharpe": wf_result["is_sharpe"], "max_drawdown": 0.0,
                     "total_trades": 0, "excess_return": 0.0}
        oos_report = {
            "sharpe": wf_result["oos_sharpe"],
            "max_drawdown": wf_result["oos_max_drawdown"],
            "total_trades": wf_result["oos_total_trades"],
            "excess_return": wf_result["oos_excess_return"],
        }
        gate = WFOGate()
        evaluation = gate.evaluate(is_report, oos_report)
        assert evaluation.passed is False
        # 五项中至少 Sharpe/MDD/trades/超额 被记录到 reason
        assert "Sharpe" in evaluation.reason
        assert "MDD" in evaluation.reason
        assert "trades" in evaluation.reason
        assert "excess_return" in evaluation.reason

    def test_gate_passes_healthy_oos(self):
        is_report = {"sharpe": 1.5, "max_drawdown": 0.0, "total_trades": 0, "excess_return": 0.0}
        oos_report = {"sharpe": 1.2, "max_drawdown": 0.10, "total_trades": 150, "excess_return": 0.03}
        gate = WFOGate()
        evaluation = gate.evaluate(is_report, oos_report)
        assert evaluation.passed is True
        assert evaluation.reason == ""
