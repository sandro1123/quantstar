"""wfo 概念覆盖守卫 (卡#44, 统筹 sm-4f56020116d4403e):

根因 (F 取证 2026-08-30, A ④ 自核实证):
  walk_forward 回看窗口内 concept_daily 缺 3 日 (2026-05-02/04/05 —
  五一节后空洞, 生产库 GROUP BY date 实证 0 行), 而
  _loader.get_concept_data 的 `except Exception: return []` + 缺日逐股
  查表天然返空表 → 零告警; strategy_engine.build_context 只在异常时
  warning, 空表静默通过 → 缺口数据零填充直达因子计算。signal/factor
  模式即假值入结果 (违宪法「宁可 NULL 不可假值」); hold/walk_forward
  走 HoldMode 只吃 kline 不消费 concept — 今晚 (hold) 无污染, 未来切
  signal/factor 则直接炸。

修法 (walk_forward 入口预检):
  run() 开头查窗口内 concept_daily 覆盖率 (data_layer.get_concept_coverage,
  对照交易日历):
  - 覆盖<100% 且该 mode 消费 concept → 硬失败, error 列缺失日期清单;
  - mode 不消费 concept → WARNING 落日志 (铁律#12), 照常跑。
  替身无 get_concept_coverage (旧测试 MagicMock 等) → 跳过预检不硬失败,
  向后兼容既有 429P 测试。

复现: fixture 交易日 5 日 concept 缺 2 日 → 现状 WalkForwardMode.run
静默跑完 (无任何覆盖检查) — RED 期望其硬失败并列缺日。
"""
import logging

from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, CostConfig
from backtest_layer.modes.walk_forward import WalkForwardConfig, WalkForwardMode
from data_layer import DataLayer


class _FakeDataLayer(DataLayer):
    """交易日由 klines 有行日期构成; concept_dates 旋钮挖缺口。

    继承真 DataLayer (不调 super().__init__ — 轻量替身): walk_forward 预检
    用 isinstance 真源判别 (MagicMock 伪造任何属性防不住, 实证打死5测试),
    替身子类化让预检路径可达, 裸替身/MagicMock 走跳过路径。
    """

    def __init__(self, dates, concept_dates):
        self._dates = set(dates)
        self._concept_dates = set(concept_dates)

    def snapshot(self, date):
        if date in self._dates:
            class _K:
                code = "000001"
                close = 10.0
                volume = 100000
            class _S:
                klines = [_K()]
            return _S()
        return None

    def get_concept_coverage(self, start_date, end_date):
        """真 loader 语义: 对照交易日历 (klines 有行日) 的 concept 覆盖。"""
        trading = sorted(d for d in self._dates if start_date <= d <= end_date)
        missing = [d for d in trading if d not in self._concept_dates]
        coverage = (len(trading) - len(missing)) / len(trading) if trading else 1.0
        return coverage, missing


class _ConceptStrategy:
    """消费 concept 的策略层 (经 strategy_engine.build_context → get_concept_data)。"""
    consumes_concept = True


class _NoConceptStrategy:
    consumes_concept = False


def _make_mode(strategy, n_windows=1):
    broker = Broker(CostConfig(initial_capital=100000))
    dates = ["2026-05-0%d" % d for d in (1, 2, 4, 5, 6)]
    data = _FakeDataLayer(dates, concept_dates=dates)  # 默认全覆盖, 测试内可改
    mode = WalkForwardMode(broker, data, strategy,
                           WalkForwardConfig(n_windows=n_windows, min_window_days=1))
    return mode, data


def _make_input():
    return BacktestInput(
        mode="walk_forward", codes=["000001"],
        date_range=("2026-05-01", "2026-05-06"), hold_days=5,
        cost=CostConfig(initial_capital=100000),
    )


def test_concept_gap_fails_hard_when_mode_consumes_concept():
    """RED: 缺 2 日 + mode 消费 concept → 硬失败, error 列缺失日期清单。"""
    mode, data = _make_mode(_ConceptStrategy())
    data._concept_dates = {"2026-05-01", "2026-05-05", "2026-05-06"}  # 缺 02/04
    rep = mode.run(_make_input())
    assert rep.wf_result and "error" in rep.wf_result, (
        "concept 覆盖缺口且 mode 消费 concept 时必须硬失败: "
        "wf_result 须带 error 列缺失日期, 不得静默跑完")
    err = str(rep.wf_result["error"])
    assert "2026-05-02" in err and "2026-05-04" in err, (
        f"error 须列缺失日期清单, got: {err}")


def test_concept_gap_warns_but_runs_when_mode_does_not_consume(caplog):
    """RED: mode 不消费 concept → 缺口只 WARNING 落日志照常跑 (铁律#12)。"""
    mode, data = _make_mode(_NoConceptStrategy())
    data._concept_dates = {"2026-05-01", "2026-05-05", "2026-05-06"}  # 缺 02/04
    with caplog.at_level(logging.WARNING, logger="backtest_layer.modes.walk_forward"):
        rep = mode.run(_make_input())
    assert not (rep.wf_result and "error" in rep.wf_result), (
        "mode 不消费 concept 时缺口只 WARNING, 不得硬失败")
    assert any("concept" in r.message.lower() for r in caplog.records
               if r.levelno >= logging.WARNING), (
        "覆盖率缺口必须以 WARNING 落日志 (铁律#12 可观测), 不得静默")


def test_full_coverage_runs_clean(caplog):
    """绿灯路径: 覆盖 100% → 正常跑, 无 error 无 WARNING。"""
    mode, _ = _make_mode(_ConceptStrategy())  # concept_dates=dates 全覆盖
    with caplog.at_level(logging.WARNING, logger="backtest_layer.modes.walk_forward"):
        rep = mode.run(_make_input())
    assert not (rep.wf_result and "error" in rep.wf_result), (
        "覆盖 100% 不得报 concept 错")
    assert not any("concept" in r.message.lower() for r in caplog.records
                   if r.levelno >= logging.WARNING), (
        "覆盖 100% 不得发 concept WARNING")


def test_no_coverage_api_backward_compat():
    """替身无 get_concept_coverage (旧测试 MagicMock) → 跳过预检不硬失败。
    既有 test_walk_forward.py 429 家族向后兼容锚。"""
    class _OldDataLayer:
        def snapshot(self, date):
            return None  # 既有测试行为: 无行情快照

    broker = Broker(CostConfig(initial_capital=100000))
    mode = WalkForwardMode(broker, _OldDataLayer(), _ConceptStrategy(),
                           WalkForwardConfig(n_windows=1, min_window_days=1))
    rep = mode.run(_make_input())
    # 无覆盖率 API = 不预检: 走原路径 (无 klines → 空 buy_results → 零交易报告)
    assert rep.total_trades == 0
    assert not (rep.wf_result and "error" in rep.wf_result and
                "concept" in str(rep.wf_result["error"])), (
        "无 get_concept_coverage 的 data_layer 须跳过预检 (向后兼容), 不得报 concept 错")
