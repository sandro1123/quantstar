"""ST-16: 策略推荐携带入场参数 — buy 信号 entry_low/entry_high/stop_loss/take_profit 非空

验收: 新产出的 buy 推荐入场参数非空; snapshot 缺 code 时 get_history 兜底;
ATR 用历史窗口真实波幅近似。参数口径: entry 区间=近期收盘价±ATR*entry_mult,
stop_loss/take_profit 按 ATR 倍数带 (ATR 缺失回退价格 2% 固定百分比带)。
"""
from unittest.mock import MagicMock

from strategy_layer import StrategyLayer, StrategyStageContext
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.selection.selector import SelectionResult


class FakeKline:
    def __init__(self, code, close, high=None, low=None):
        self.code = code
        self.close = close
        self.high = high if high is not None else close * 1.02
        self.low = low if low is not None else close * 0.98


class FakeSnapshot:
    def __init__(self, klines):
        self.klines = klines


class FakeHistory:
    def __init__(self, klines):
        self.klines = klines


def _make_layer(snapshot_klines=None, history_map=None, snapshot_raises=False):
    layer = StrategyLayer()
    dl = MagicMock()
    if snapshot_raises:
        dl.snapshot.side_effect = RuntimeError("snapshot timeout")
    else:
        dl.snapshot.return_value = FakeSnapshot(snapshot_klines or [])
    def _hist(code, date, window):
        return FakeHistory((history_map or {}).get(code, []))
    dl.get_history.side_effect = _hist
    layer.data_layer = dl
    return layer


def _ctx_with(codes_scores):
    cands = [FactorDaily(code=c, date="2026-08-18", composite_score=s, rank=i + 1)
             for i, (c, s) in enumerate(codes_scores)]
    return StrategyStageContext(
        date="2026-08-18",
        selection=SelectionResult(date="2026-08-18", candidates=cands),
    )


def test_buy_signal_entry_params_nonempty_from_snapshot():
    """验收: snapshot 有价 → buy 推荐四参数非空"""
    layer = _make_layer(snapshot_klines=[FakeKline("000001.SZ", 10.0)])
    ctx = _ctx_with([("000001.SZ", 1.5)])
    layer._stage_signal_gen(ctx)
    buys = [s for s in ctx.signals if s.action == "buy"]
    assert len(buys) == 1
    s = buys[0]
    assert s.entry_low > 0
    assert s.entry_high > s.entry_low
    assert s.stop_loss > 0
    assert s.take_profit > s.entry_high


def test_buy_signal_entry_params_fallback_to_history():
    """验收: snapshot 缺 code → get_history 最近收盘价兜底, 参数仍非空"""
    # snapshot 返回空 (未覆盖该 code), history 提供 K 线
    hist_klines = [FakeKline("000002.SZ", 20.0, high=20.5, low=19.5)]
    layer = _make_layer(snapshot_klines=[], history_map={"000002.SZ": hist_klines})
    ctx = _ctx_with([("000002.SZ", 1.2)])
    layer._stage_signal_gen(ctx)
    buys = [s for s in ctx.signals if s.action == "buy"]
    assert len(buys) == 1
    s = buys[0]
    assert s.entry_low > 0, "snapshot 缺 code 时应由 history 兜底价格"
    assert s.entry_high > s.entry_low
    assert s.stop_loss > 0
    assert s.take_profit > 0


def test_buy_signal_entry_params_when_snapshot_raises():
    """验收: snapshot 整体超时 → history 兜底, 不崩溃且参数非空"""
    hist_klines = [FakeKline("000003.SZ", 15.0, high=15.3, low=14.7)]
    layer = _make_layer(snapshot_raises=True, history_map={"000003.SZ": hist_klines})
    ctx = _ctx_with([("000003.SZ", 1.0)])
    layer._stage_signal_gen(ctx)
    buys = [s for s in ctx.signals if s.action == "buy"]
    assert len(buys) == 1
    assert buys[0].entry_low > 0


def test_atr_from_history_window():
    """验收: ATR 用历史窗口 mean(high-low) 近似, stop/take 按 ATR 倍数带"""
    # high-low = 1.0 恒定 → ATR ≈ 1.0
    hist_klines = [FakeKline("000004.SZ", 10.0, high=10.5, low=9.5) for _ in range(5)]
    layer = _make_layer(snapshot_klines=[FakeKline("000004.SZ", 10.0)],
                        history_map={"000004.SZ": hist_klines})
    ctx = _ctx_with([("000004.SZ", 1.5)])
    layer._stage_signal_gen(ctx)
    s = [x for x in ctx.signals if x.action == "buy"][0]
    # 默认 atr_multiplier: entry=0.5, stop=2, profit=3
    assert abs(s.stop_loss - (10.0 - 2 * 1.0)) < 0.05
    assert abs(s.take_profit - (10.0 + 3 * 1.0)) < 0.05


def test_no_data_layer_graceful():
    """无 DataLayer → 不崩溃, 参数为 0 (诚实, 不伪造)"""
    layer = StrategyLayer()
    layer.data_layer = None
    ctx = _ctx_with([("000005.SZ", 1.0)])
    layer._stage_signal_gen(ctx)
    buys = [s for s in ctx.signals if s.action == "buy"]
    assert len(buys) == 1
    assert buys[0].entry_low == 0.0  # 无数据源, 诚实为 0


def test_sell_signal_no_entry_params():
    """sell 信号不携带入场参数 (仅 buy 需要)"""
    layer = _make_layer(snapshot_klines=[FakeKline("000001.SZ", 10.0)])
    ctx = _ctx_with([("000001.SZ", 1.5)])
    ctx.current_positions = {"000009.SZ": {"status": "active"}}
    layer._stage_signal_gen(ctx)
    sells = [s for s in ctx.signals if s.action == "sell"]
    assert len(sells) == 1
    assert sells[0].entry_low == 0.0
