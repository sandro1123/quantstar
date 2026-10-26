"""BT-04: 基准对比真接线 — TDD 回归测试

核心纪律 (04 审计 BT-04 卡):
1. 指数可得 → benchmark_return 真实非零, excess = 策略收益 - 基准收益 (对齐窗口真算)
2. 指数缺失/不足 → 基准系字段全部降级 None + 标注"基准不可用",
   绝不回落 0 假值 (excess_return=total_return 假等式必须消失)

数据契约: DL-23 get_index_daily(code, start_date, end_date) →
[{date, open, high, low, close, volume, name}] 按日期升序; 表不存在返回空。
注: DL-23 尚在 fix/data-batch 待合并, 本测试用 fake data_layer 模拟该契约,
合并后生产链路自动接通; 缺失场景与现状一致, 降级逻辑时时生效。
"""
from types import SimpleNamespace

import pytest

from backtest_layer.core.benchmark import Benchmark, resolve_benchmark_name
from backtest_layer.core.engine import BacktestEngine
from backtest_layer.core.models import BacktestInput, BacktestReport, CostConfig
from backtest_layer.modes.walk_forward import WalkForwardConfig, WalkForwardMode

DATE_RANGE = ("2026-08-03", "2026-08-07")
DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
INDEX_CODE_CSI300 = "000300.SH"
INDEX_CODE_CHINEXT = "399006.SZ"
# 沪深300 窗口: 100 → 105 (+5%), 途中回撤 101 → 99
INDEX_CLOSES = [100.0, 101.0, 99.0, 102.0, 105.0]
INDEX_MDD = (101.0 - 99.0) / 101.0


def _index_rows(code):
    """模拟 DL-23 index_daily 入库的两条指数 (同曲线, 便于区分代码路由)"""
    if code not in (INDEX_CODE_CSI300, INDEX_CODE_CHINEXT):
        return []
    return [{"date": d, "close": c, "name": code} for d, c in zip(DATES, INDEX_CLOSES)]


class FakeKline:
    def __init__(self, code, close, volume=10_000_000):
        self.code = code
        self.close = close
        self.open = close  # next_open 默认成交口径需开盘价
        self.volume = volume


class FakeIndexDataLayer:
    """具备 DL-23 get_index_daily 契约 + 按日 snapshot 的假数据层"""

    def __init__(self, closes_by_date=None):
        self._closes_by_date = closes_by_date or {d: {"600519": 10.0} for d in DATES}

    def snapshot(self, date):
        closes = self._closes_by_date.get(date, {})
        return SimpleNamespace(klines=[FakeKline(c, p) for c, p in closes.items()])

    def get_index_daily(self, code, start_date="", end_date=""):
        rows = _index_rows(code)
        return [r for r in rows
                if (not start_date or r["date"] >= start_date)
                and (not end_date or r["date"] <= end_date)]


class NoIndexDataLayer(FakeIndexDataLayer):
    """DL-23 读 API 不存在 (合并前现状): get_index_daily 非 callable → 必须诚实降级"""
    get_index_daily = None


class EmptyIndexDataLayer(FakeIndexDataLayer):
    """index_daily 表为空 / 窗口无数据"""

    def get_index_daily(self, code, start_date="", end_date=""):
        return []


def _hold_input():
    return BacktestInput(mode="hold", codes=["600519"], date_range=DATE_RANGE,
                         cost=CostConfig(initial_capital=105_500.0, slippage_rate=0.0))


class TestResolveBenchmarkName:
    def test_default_is_csi300(self):
        assert resolve_benchmark_name(None, None) == "csi300"

    def test_explicit_wins_over_config(self):
        layer = SimpleNamespace(config=SimpleNamespace(
            get=lambda k, d=None: "chinext"))
        assert resolve_benchmark_name("csi300", layer) == "csi300"

    def test_auto_reads_config(self):
        layer = SimpleNamespace(config=SimpleNamespace(
            get=lambda k, d=None: "chinext" if k == "backtest.benchmark_index" else d))
        assert resolve_benchmark_name("auto", layer) == "chinext"
        assert resolve_benchmark_name("", layer) == "chinext"


class TestBenchmarkIndexCurve:
    def test_fetch_returns_normalized_nav(self):
        b = Benchmark("csi300")
        curve = b.fetch_benchmark_curve(DATE_RANGE, FakeIndexDataLayer())
        assert len(curve) == 5
        assert curve[0]["nav"] == pytest.approx(1.0)
        assert curve[-1]["nav"] == pytest.approx(1.05)

    def test_index_code_mapping(self):
        assert Benchmark("csi300").index_code == INDEX_CODE_CSI300
        assert Benchmark("chinext").index_code == INDEX_CODE_CHINEXT
        assert Benchmark("000300.SH").index_code == INDEX_CODE_CSI300
        assert Benchmark("unknown_bench").index_code is None

    def test_fetch_degrades_without_api(self):
        assert Benchmark("csi300").fetch_benchmark_curve(DATE_RANGE, NoIndexDataLayer()) == []

    def test_fetch_degrades_without_data_layer(self):
        assert Benchmark("csi300").fetch_benchmark_curve(DATE_RANGE, None) == []


class TestEngineBenchmarkReal:
    """指数可得 → 真超额 (纪律: excess != total_return)"""

    def test_hold_report_has_real_benchmark(self):
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=FakeIndexDataLayer(),
                            strategy_layer=None)
        # 基准收益 = 指数窗口收益真算, 非零
        assert report.benchmark_return == pytest.approx(0.05)
        assert report.benchmark_return != 0.0
        # 超额 = 对齐窗口策略收益 - 基准收益; 策略净值曲线覆盖同窗口 → 与总收益一致
        assert report.excess_return == pytest.approx(
            report.total_return - report.benchmark_return, abs=1e-6)
        assert report.excess_return != report.total_return  # 假等式必须消失
        # 基准最大回撤 = 101 → 99
        assert report.benchmark_max_drawdown == pytest.approx(INDEX_MDD, abs=1e-6)
        # 相对最大回撤 (超额净值曲线) 可算
        assert report.excess_max_drawdown is not None
        # IR 从逐日超额真算
        assert report.information_ratio is not None
        assert report.information_ratio != 0.0 or report.excess_max_drawdown == 0.0
        # 对比段信息完整
        comp = report.benchmark_comparison
        assert comp["available"] is True
        assert comp["index_code"] == INDEX_CODE_CSI300
        assert report.benchmark_index == INDEX_CODE_CSI300

    def test_config_selects_chinext(self):
        layer = FakeIndexDataLayer()
        layer.config = SimpleNamespace(
            get=lambda k, d=None: "chinext" if k == "backtest.benchmark_index" else d)
        inp = _hold_input()
        inp.benchmark = "auto"
        engine = BacktestEngine()
        report = engine.run(inp, data_layer=layer, strategy_layer=None)
        assert report.benchmark_return == pytest.approx(0.05)
        assert report.benchmark_comparison["index_code"] == INDEX_CODE_CHINEXT

    def test_attach_without_dated_curve_only_fills_window_metrics(self):
        """signal 模式净值曲线无日期 → 窗口型指标真算, 相对指标降级 None"""
        rep = BacktestReport(mode="signal", date_range=DATE_RANGE,
                             total_return=0.01, annual_return=0.0, sharpe=0.0,
                             max_drawdown=0.0, win_rate=0.0, total_trades=0,
                             equity_curve=[100_000.0, 101_000.0])
        ok = Benchmark("csi300").attach_to_report(rep, FakeIndexDataLayer())
        assert ok is True
        assert rep.benchmark_return == pytest.approx(0.05)
        assert rep.benchmark_max_drawdown == pytest.approx(INDEX_MDD, abs=1e-6)
        # 超额 = 策略总收益 - 基准收益 (同窗口标量真算, 与有无日期序列无关)
        assert rep.excess_return == pytest.approx(0.01 - 0.05)
        # 无日期重叠 → IR/相对回撤拒绝编造
        assert rep.information_ratio is None
        assert rep.excess_max_drawdown is None
        assert rep.benchmark_comparison["available"] is True
        assert rep.benchmark_comparison.get("note")


class TestBenchmarkDegradeHonest:
    """灵魂纪律: 指数缺失 → None + 标注, 绝不回落 0 假值"""

    def test_no_api_degrades_to_none(self):
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=NoIndexDataLayer(),
                            strategy_layer=None)
        assert report.benchmark_return is None
        assert report.excess_return is None
        assert report.information_ratio is None
        assert report.benchmark_max_drawdown is None
        assert report.excess_max_drawdown is None
        comp = report.benchmark_comparison
        assert comp["available"] is False
        assert comp["reason"]
        assert any("基准不可用" in w for w in report.data_quality_warnings)
        # 策略自身指标不受基准缺失影响
        assert report.total_trades == 1
        assert isinstance(report.total_return, float)

    def test_empty_index_table_degrades_to_none(self):
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=EmptyIndexDataLayer(),
                            strategy_layer=None)
        assert report.benchmark_return is None
        assert report.excess_return is None
        assert report.benchmark_comparison["available"] is False
        assert any("基准不可用" in w for w in report.data_quality_warnings)

    def test_single_index_point_degrades(self):
        class OneRowLayer(FakeIndexDataLayer):
            def get_index_daily(self, code, start_date="", end_date=""):
                return [{"date": DATES[0], "close": 100.0, "name": code}]

        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=OneRowLayer(),
                            strategy_layer=None)
        assert report.benchmark_return is None
        assert report.benchmark_comparison["available"] is False

    def test_unavailable_note_not_duplicated(self):
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=NoIndexDataLayer(),
                            strategy_layer=None)
        warns = [w for w in report.data_quality_warnings if "基准不可用" in w]
        assert len(warns) == 1


class TestHTMLReportBenchmark:
    def test_html_shows_unavailable_note(self):
        from backtest_layer.results.report import HTMLReportBuilder
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=NoIndexDataLayer(),
                            strategy_layer=None)
        html = HTMLReportBuilder.build(report)
        assert "基准不可用" in html

    def test_html_renders_none_benchmark_without_crash(self):
        from backtest_layer.results.report import HTMLReportBuilder
        rep = BacktestReport(mode="hold", date_range=DATE_RANGE,
                             total_return=0.02, annual_return=0.0, sharpe=0.0,
                             max_drawdown=0.0, win_rate=0.0, total_trades=0)
        assert rep.benchmark_return is None  # 默认即"不可算", 非 0 假值
        html = HTMLReportBuilder.build(rep)
        assert "<!DOCTYPE html>" in html

    def test_html_shows_real_benchmark_section(self):
        from backtest_layer.results.report import HTMLReportBuilder
        engine = BacktestEngine()
        report = engine.run(_hold_input(), data_layer=FakeIndexDataLayer(),
                            strategy_layer=None)
        html = HTMLReportBuilder.build(report)
        assert "基准对比" in html
        assert "5.00%" in html  # 基准收益 +5% 真显示


class TestWalkForwardBenchmark:
    def test_wf_with_index_real_window_excess(self):
        from backtest_layer.core.broker import Broker
        wf = WalkForwardMode(Broker(CostConfig(initial_capital=105_500.0,
                                              slippage_rate=0.0)),
                             FakeIndexDataLayer(), None,
                             WalkForwardConfig(n_windows=2, min_window_days=2))
        inp = BacktestInput(mode="walk_forward", codes=["600519"],
                            date_range=DATE_RANGE,
                            cost=CostConfig(initial_capital=105_500.0,
                                            slippage_rate=0.0))
        report = wf.run(inp)
        assert "oos_excess_return" in report.wf_result
        # 有指数数据 → 子窗口真算超额, 聚合为真实数值
        assert isinstance(report.wf_result["oos_excess_return"], float)
        assert report.benchmark_return is not None

    def test_wf_with_magicmock_layer_none_safe(self):
        """回归: 老 _mean 对 None 求 sum 会崩 → 聚合必须 None 安全"""
        from unittest.mock import MagicMock

        from backtest_layer.core.broker import Broker
        wf = WalkForwardMode(Broker(CostConfig(initial_capital=105_500.0)),
                             MagicMock(), MagicMock(),
                             WalkForwardConfig(n_windows=2, min_window_days=2))
        inp = BacktestInput(mode="walk_forward", codes=["600519"],
                            date_range=DATE_RANGE)
        report = wf.run(inp)
        assert report.wf_result["window_count"] == 2
        assert report.wf_result["oos_excess_return"] is None
        assert report.benchmark_return is None
        assert report.excess_return is None


class TestWFONoneExcessGate:
    """wfo_gate: excess=None 时不得 TypeError, 需下限>0 时 fail-closed"""

    def test_gate_none_excess_with_positive_min_fails_closed(self):
        from backtest_layer.feedback.wfo_gate import WFOGate
        gate = WFOGate(min_sharpe=0.0, max_drawdown=1.0, min_trades=0,
                       min_excess_return=0.02)
        ev = gate.evaluate({"sharpe": 1.0},
                           {"sharpe": 1.0, "max_drawdown": 0.1,
                            "total_trades": 10, "excess_return": None})
        assert ev.passed is False
        assert "excess_return" in ev.reason

    def test_gate_none_excess_with_zero_min_skips(self):
        from backtest_layer.feedback.wfo_gate import WFOGate
        gate = WFOGate(min_sharpe=0.0, max_drawdown=1.0, min_trades=0,
                       min_excess_return=0.0)
        ev = gate.evaluate({"sharpe": 1.0},
                           {"sharpe": 1.0, "max_drawdown": 0.1,
                            "total_trades": 10, "excess_return": None})
        assert ev.passed is True