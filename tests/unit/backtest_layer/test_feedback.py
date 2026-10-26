"""IC Feedback + WFO Gate 单元测试 (BT-05: IC 双制共存 + 三档动作)"""
import sqlite3

from backtest_layer.feedback.ic_feedback import (
    ICFeedback, build_ic_report_from_db,
)
from backtest_layer.feedback.param_rules import AdaptiveParamTuner
from backtest_layer.feedback.wfo_gate import WFOGate


class TestICFeedback:
    def test_analyze_keep(self):
        ic = ICFeedback()
        result = ic.analyze("momentum", [0.05, 0.04, 0.06, 0.03, 0.05])
        assert result.recommendation == "keep"

    def test_analyze_drop(self):
        ic = ICFeedback()
        result = ic.analyze("noise", [0.001, 0.002, 0.001, 0.003, 0.001])
        assert result.recommendation == "drop"

    def test_analyze_watch(self):
        ic = ICFeedback()
        result = ic.analyze("weak", [0.015, 0.012, 0.018, 0.010, 0.014])
        assert result.recommendation == "watch"

    def test_analyze_empty(self):
        ic = ICFeedback()
        result = ic.analyze("empty", [])
        assert result.recommendation == "no_data"
        assert result.n_obs == 0

    def test_generate_report(self):
        """BT-05: 只评估不调权 — 报告分 strong/weak, 不改任何权重"""
        ic = ICFeedback()
        report = ic.generate_report({
            "momentum": [0.05, 0.04, 0.06, 0.03, 0.05],
            "noise": [0.001, 0.002, 0.001, 0.003, 0.001],
        })
        assert report.n_factors == 2
        assert "momentum" in report.strong
        assert "noise" in report.weak_or_invalid
        d = report.to_dict()
        assert d["analyses"]["momentum"]["recommendation"] == "keep"

    def test_generate_report_empty(self):
        report = ICFeedback().generate_report({})
        assert report.n_factors == 0
        assert report.strong == []

    def test_no_adjust_weights(self):
        """BT-05 (D-16): 调权职责归策略层 ICTuner, 回测层 ICFeedback 不再有 adjust_weights"""
        assert not hasattr(ICFeedback(), "adjust_weights")


class _Db:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self._conn


class TestBuildICReportFromDb:
    def test_from_factor_ic(self):
        db = _Db()
        conn = db.get_connection()
        conn.execute("CREATE TABLE factor_ic (calc_date TEXT, factor_name TEXT, mean_ic REAL)")
        for d, ic in [("2026-08-01", 0.05), ("2026-08-02", 0.06), ("2026-08-03", 0.04)]:
            conn.execute("INSERT INTO factor_ic VALUES (?, 'momentum', ?)", (d, ic))
        conn.commit()
        report = build_ic_report_from_db(db, lookback_days=3650)
        assert report.n_factors == 1
        assert "momentum" in report.analyses

    def test_no_table_returns_empty(self):
        report = build_ic_report_from_db(_Db(), lookback_days=30)
        assert report.n_factors == 0

    def test_no_db_returns_empty(self):
        report = build_ic_report_from_db(None)
        assert report.n_factors == 0


class TestWFOGate:
    def _is_report(self, sharpe=1.0):
        return {"sharpe": sharpe, "max_drawdown": 0.10, "total_trades": 150}

    def _oos_report(self, sharpe=0.8, max_dd=0.10, trades=150, excess=0.02):
        return {"sharpe": sharpe, "max_drawdown": max_dd, "total_trades": trades, "excess_return": excess}

    def test_pass(self):
        gate = WFOGate(min_sharpe=0.5, min_trades=100, min_excess_return=0.0)
        result = gate.evaluate(self._is_report(1.0), self._oos_report(0.8, trades=120))
        assert result.passed is True

    def test_fail_low_sharpe(self):
        gate = WFOGate(min_sharpe=0.5, min_trades=0, max_drawdown=0.50)
        result = gate.evaluate(self._is_report(1.0), self._oos_report(0.3))
        assert result.passed is False

    def test_fail_high_degradation(self):
        gate = WFOGate(min_sharpe=0.0, min_trades=0, max_drawdown=0.50)
        result = gate.evaluate(self._is_report(1.0), self._oos_report(0.4))
        assert result.passed is False

    def test_fail_high_mdd(self):
        gate = WFOGate(min_sharpe=0.0, min_trades=0, max_drawdown=0.15)
        result = gate.evaluate(self._is_report(1.0), self._oos_report(0.8, max_dd=0.25))
        assert result.passed is False
        assert "MDD" in result.reason

    def test_fail_low_trades(self):
        gate = WFOGate(min_sharpe=0.0, min_trades=200, max_drawdown=0.50)
        result = gate.evaluate(self._is_report(1.0), self._oos_report(0.8, trades=50))
        assert result.passed is False
        assert "trades" in result.reason


class TestWFOTier:
    """BT-05 (D-17): 三档动作分级; BT-11 ⑤ (B-13): 效率比唯一口径透传"""

    def _gate(self):
        return WFOGate(min_sharpe=0.5, min_trades=100, min_excess_return=0.0)

    def test_passed_deploy(self):
        gate = self._gate()
        ev = gate.evaluate({"sharpe": 1.0}, {"sharpe": 0.8, "total_trades": 120, "excess_return": 0.02},
                           oos_efficiency=0.85)
        assert ev.passed
        assert gate.tier(ev) == "deploy"

    def test_mid_eff_paper_trading(self):
        gate = self._gate()
        ev = gate.evaluate({"sharpe": 1.0}, {"sharpe": 0.3, "total_trades": 120, "excess_return": 0.02},
                           oos_efficiency=0.62)
        assert not ev.passed
        assert gate.tier(ev) == "paper_trading"

    def test_low_eff_hold(self):
        gate = self._gate()
        ev = gate.evaluate({"sharpe": 1.0}, {"sharpe": 0.2, "total_trades": 120, "excess_return": 0.02},
                           oos_efficiency=0.31)
        assert not ev.passed
        assert gate.tier(ev) == "hold"

    def test_boundary_05_is_paper(self):
        gate = self._gate()
        ev = gate.evaluate({"sharpe": 1.0}, {"sharpe": 0.3, "total_trades": 120, "excess_return": 0.02},
                           oos_efficiency=0.5)
        assert gate.tier(ev) == "paper_trading"

    def test_missing_efficiency_failed_is_hold_unevaluated(self):
        """BT-11 ⑤ (B-13) + G5 (2026-10-23): 效率比缺失 + 未过门禁 →

        G5 修正: 原断言 "hold", 但「从未评估」与「评估了但不过」是两件事。
        实证: backtest_runs 24 条, wf_efficiency 非空 0 条 => 从未评估,
        而告警却写「低于 0.5, 策略可能过拟合」。

        保守行为不变(一律 hold, 不拿默认值分级), 但性质分开:
          oos_efficiency is None -> hold_unevaluated
          oos_efficiency < 0.5   -> hold
        """
        gate = self._gate()
        ev = gate.evaluate({"sharpe": 1.0}, {"sharpe": 0.3, "total_trades": 120, "excess_return": 0.02})
        assert ev.oos_efficiency is None
        assert gate.tier(ev) == "hold_unevaluated", (
            "未评估必须是独立档, 不得与真不过共用 hold")


class TestAdaptiveParamTuner:
    """BT-05: 规则引擎参数调优 (设计 §10.2)"""

    def test_low_win_rate_tightens(self):
        t = AdaptiveParamTuner()
        s = t.suggest({"win_rate": 0.30, "profit_loss_ratio": 1.5, "n_samples": 20})
        assert s.buy_threshold == 75  # 65 + 10
        assert s.stop_loss_pct > -0.08  # 收紧 (绝对值变小)
        assert any("win_rate" in a for a in s.adjustments)

    def test_high_win_rate_loosens(self):
        t = AdaptiveParamTuner()
        s = t.suggest({"win_rate": 0.60, "profit_loss_ratio": 1.5, "n_samples": 20})
        assert s.buy_threshold == 60  # 65 - 5
        assert s.stop_loss_pct < -0.08  # 放开

    def test_low_plr_tightens(self):
        t = AdaptiveParamTuner()
        s = t.suggest({"win_rate": 0.45, "profit_loss_ratio": 0.8, "n_samples": 20})
        assert s.buy_threshold == 65  # 胜率居中不动
        assert s.stop_loss_pct > -0.08  # 盈亏比差 → 收紧
        assert any("profit_loss_ratio" in a for a in s.adjustments)

    def test_bear_regime_tightens(self):
        t = AdaptiveParamTuner()
        s = t.suggest({"win_rate": 0.45, "profit_loss_ratio": 1.5, "n_samples": 20}, regime="bear")
        assert s.stop_loss_pct > -0.08
        assert s.regime == "bear"

    def test_no_performance_keeps_base(self):
        t = AdaptiveParamTuner()
        s = t.suggest({})
        assert s.buy_threshold == 65
        assert s.stop_loss_pct == -0.08
        assert any("no_performance_data" in a for a in s.adjustments)

    def test_to_dict(self):
        s = AdaptiveParamTuner().suggest({"win_rate": 0.30, "profit_loss_ratio": 1.5, "n_samples": 20})
        d = s.to_dict()
        assert d["buy_threshold"] == 75
        assert d["n_samples"] == 20
