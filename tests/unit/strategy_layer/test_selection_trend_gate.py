"""L1 趋势闸门测试 — 候选池过滤 MA5<MA10<MA20 空头排列 (与 exit layer4 对称口径)

背景: 2026-08-28 模拟盘首日, 反转因子主导的入场模型选出的 10 只全部处于
均线空头排列, 入场次日即被 exit layer4 全灭。入场端须加趋势闸门。
"""
from strategy_layer.selection.selector import SelectionPipeline
from strategy_layer.aggregation.aggregator import FactorDaily

DATE = "2026-07-24"  # 周五


class FakeKline:
    def __init__(self, code, date, close, amount=1e8):
        self.code = code
        self.date = date
        self.close = close
        self.amount = amount


class FakeSnap:
    def __init__(self, klines):
        self.klines = klines


class FakeDataLayer:
    def __init__(self, klines_by_code, list_date="2020-01-01"):
        self._k = klines_by_code
        self._ld = list_date

    def snapshot_range(self, start, end):
        allk = []
        for ks in self._k.values():
            allk += [k for k in ks if start <= k.date <= end]
        return [FakeSnap(allk)]

    def get_stock_info(self, code):
        class Info:
            pass
        info = Info()
        info.list_date = self._ld
        return info


def _trading_dates(n, end=DATE):
    """end 往前取 n 个工作日 (近似交易日)"""
    from datetime import datetime, timedelta
    out = []
    dt = datetime.strptime(end, "%Y-%m-%d")
    while len(out) < n:
        if dt.weekday() < 5:
            out.append(dt.strftime("%Y-%m-%d"))
        dt -= timedelta(days=1)
    return sorted(out)


def _klines(code, closes):
    dates = _trading_dates(len(closes))
    return [FakeKline(code, d, c) for d, c in zip(dates, closes)]


def _fd(code, score=0.5):
    return FactorDaily(code=code, date=DATE, composite_score=score, factor_scores={})


BEARISH = [30 - i * 0.3 for i in range(25)]          # 递减 → MA5<MA10<MA20
BULLISH = [20 + i * 0.3 for i in range(25)]          # 递增 → 多头排列


class TestTrendGate:
    def test_bearish_alignment_filtered_by_default(self):
        dl = FakeDataLayer({"000001": _klines("000001", BEARISH),
                            "000002": _klines("000002", BULLISH)})
        pipe = SelectionPipeline(data_layer=dl)
        result = pipe.run([_fd("000001"), _fd("000002")])
        codes = [c.code for c in result.candidates]
        assert "000001" not in codes, "空头排列票应被趋势闸门过滤"
        assert "000002" in codes
        assert any("趋势闸门" in f for f in result.filters_applied)

    def test_gate_disabled_by_config_keeps_bearish(self):
        dl = FakeDataLayer({"000001": _klines("000001", BEARISH)})
        pipe = SelectionPipeline(config={"strategy.selection.l1.trend_gate": False},
                                 data_layer=dl)
        result = pipe.run([_fd("000001")])
        assert [c.code for c in result.candidates] == ["000001"]

    def test_insufficient_klines_fail_open(self):
        dl = FakeDataLayer({"000001": _klines("000001", BEARISH[:10])})
        pipe = SelectionPipeline(data_layer=dl)
        result = pipe.run([_fd("000001")])
        assert [c.code for c in result.candidates] == ["000001"], \
            "kline 不足 20 根时应 fail-open 放行"
